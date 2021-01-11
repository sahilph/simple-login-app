"""
Handle the email *forward* and *reply*. phase. There are 3 actors:
- contact: who sends emails to alias@sl.co address
- SL email handler (this script)
- user personal email: to be protected. Should never leak to contact.

This script makes sure that in the forward phase, the email that is forwarded to user personal email has the following
envelope and header fields:
Envelope:
    mail from: @contact
    rcpt to: @personal_email
Header:
    From: @contact
    To: alias@sl.co # so user knows this email is sent to alias
    Reply-to: special@sl.co # magic HERE

And in the reply phase:
Envelope:
    mail from: @contact
    rcpt to: @contact

Header:
    From: alias@sl.co # so for contact the email comes from alias. magic HERE
    To: @contact

The special@sl.co allows to hide user personal email when user clicks "Reply" to the forwarded email.
It should contain the following info:
- alias
- @contact


"""
import argparse
import asyncio
import email
import os
import time
import uuid
from email import encoders
from email.encoders import encode_noop
from email.message import Message
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, make_msgid, formatdate, getaddresses
from io import BytesIO
from smtplib import SMTP, SMTPRecipientsRefused, SMTPServerDisconnected
from typing import List, Tuple, Optional

import aiosmtpd
import aiospamc
import arrow
import spf
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import Envelope
from sqlalchemy.exc import IntegrityError

from app import pgp_utils, s3
from app.alias_utils import try_auto_create
from app.config import (
    EMAIL_DOMAIN,
    POSTFIX_SERVER,
    URL,
    POSTFIX_SUBMISSION_TLS,
    UNSUBSCRIBER,
    LOAD_PGP_EMAIL_HANDLER,
    ENFORCE_SPF,
    ALERT_REVERSE_ALIAS_UNKNOWN_MAILBOX,
    ALERT_BOUNCE_EMAIL,
    ALERT_SPAM_EMAIL,
    ALERT_SPF,
    POSTFIX_PORT,
    SENDER,
    SENDER_DIR,
    SPAMASSASSIN_HOST,
    MAX_SPAM_SCORE,
    MAX_REPLY_PHASE_SPAM_SCORE,
    ALERT_SEND_EMAIL_CYCLE,
    ALERT_MAILBOX_IS_ALIAS,
    PGP_SENDER_PRIVATE_KEY,
    ALERT_BOUNCE_EMAIL_REPLY_PHASE,
    NOREPLY,
    BOUNCE_EMAIL,
    BOUNCE_PREFIX,
    BOUNCE_SUFFIX,
)
from app.email_utils import (
    send_email,
    add_dkim_signature,
    add_or_replace_header,
    delete_header,
    render,
    get_orig_message_from_bounce,
    delete_all_headers_except,
    get_spam_info,
    get_orig_message_from_spamassassin_report,
    parseaddr_unicode,
    send_email_with_rate_control,
    get_email_domain_part,
    copy,
    to_bytes,
    send_email_at_most_times,
    is_valid_alias_address_domain,
    should_add_dkim_signature,
    add_header,
    get_header_unicode,
    generate_reply_email,
    is_reply_email,
    normalize_reply_email,
    is_valid_email,
    replace,
    should_disable,
    get_header_from_bounce,
    parse_email_log_id_from_bounce,
)
from app.extensions import db
from app.greylisting import greylisting_needed
from app.log import LOG
from app.models import (
    Alias,
    Contact,
    EmailLog,
    User,
    RefusedEmail,
    Mailbox,
)
from app.pgp_utils import PGPException, sign_data_with_pgpy, sign_data
from app.spamassassin_utils import SpamAssassin
from app.utils import random_string, sanitize_email
from init_app import load_pgp_public_keys
from server import create_app, create_light_app

# forward or reply
_DIRECTION = "X-SimpleLogin-Type"

_IP_HEADER = "X-SimpleLogin-Client-IP"
_EMAIL_LOG_ID_HEADER = "X-SimpleLogin-EmailLog-ID"
_ENVELOPE_FROM = "X-SimpleLogin-Envelope-From"

_MIME_HEADERS = [
    "MIME-Version",
    "Content-Type",
    "Content-Disposition",
    "Content-Transfer-Encoding",
]
_MIME_HEADERS = [h.lower() for h in _MIME_HEADERS]


# fix the database connection leak issue
# use this method instead of create_app
def new_app():
    app = create_light_app()

    @app.teardown_appcontext
    def shutdown_session(response_or_exc):
        # same as shutdown_session() in flask-sqlalchemy but this is not enough
        db.session.remove()

        # dispose the engine too
        db.engine.dispose()

    return app


def get_or_create_contact(from_header: str, mail_from: str, alias: Alias) -> Contact:
    """
    contact_from_header is the RFC 2047 format FROM header
    """
    contact_name, contact_email = parseaddr_unicode(from_header)
    if not is_valid_email(contact_email):
        # From header is wrongly formatted, try with mail_from
        if mail_from and mail_from != "<>":
            LOG.warning(
                "Cannot parse email from from_header %s, parse from mail_from %s",
                from_header,
                mail_from,
            )
            _, contact_email = parseaddr_unicode(mail_from)

    if not is_valid_email(contact_email):
        LOG.warning(
            "invalid contact email %s. Parse from %s %s",
            contact_email,
            from_header,
            mail_from,
        )
        # either reuse a contact with empty email or create a new contact with empty email
        contact_email = ""

    contact_email = sanitize_email(contact_email)

    contact = Contact.get_by(alias_id=alias.id, website_email=contact_email)
    if contact:
        if contact.name != contact_name:
            LOG.d(
                "Update contact %s name %s to %s",
                contact,
                contact.name,
                contact_name,
            )
            contact.name = contact_name
            db.session.commit()

        # contact created in the past does not have mail_from and from_header field
        if not contact.mail_from and mail_from:
            LOG.d(
                "Set contact mail_from %s: %s to %s",
                contact,
                contact.mail_from,
                mail_from,
            )
            contact.mail_from = mail_from
            db.session.commit()

        if not contact.from_header and from_header:
            LOG.d(
                "Set contact from_header %s: %s to %s",
                contact,
                contact.from_header,
                from_header,
            )
            contact.from_header = from_header
            db.session.commit()
    else:
        LOG.d(
            "create contact %s for alias %s",
            contact_email,
            alias,
        )

        try:
            contact = Contact.create(
                user_id=alias.user_id,
                alias_id=alias.id,
                website_email=contact_email,
                name=contact_name,
                mail_from=mail_from,
                from_header=from_header,
                reply_email=generate_reply_email(contact_email, alias.user)
                if is_valid_email(contact_email)
                else NOREPLY,
            )
            if not contact_email:
                LOG.d("Create a contact with invalid email for %s", alias)
                contact.invalid_email = True

            db.session.commit()
        except IntegrityError:
            LOG.warning("Contact %s %s already exist", alias, contact_email)
            db.session.rollback()
            contact = Contact.get_by(alias_id=alias.id, website_email=contact_email)

    return contact


def replace_header_when_forward(msg: Message, alias: Alias, header: str):
    """
    Replace CC or To header by Reply emails in forward phase
    """
    new_addrs: [str] = []
    headers = msg.get_all(header, [])
    for contact_name, contact_email in getaddresses(headers):
        # convert back to original then parse again to make sure contact_name is unicode
        addr = formataddr((contact_name, contact_email))
        contact_name, _ = parseaddr_unicode(addr)

        contact_email = sanitize_email(contact_email)

        # no transformation when alias is already in the header
        if contact_email == alias.email:
            new_addrs.append(addr)
            continue

        if not is_valid_email(contact_email):
            LOG.warning("invalid contact email %s. %s. Skip", contact_email, headers)
            continue

        contact = Contact.get_by(alias_id=alias.id, website_email=contact_email)
        if contact:
            # update the contact name if needed
            if contact.name != contact_name:
                LOG.d(
                    "Update contact %s name %s to %s",
                    contact,
                    contact.name,
                    contact_name,
                )
                contact.name = contact_name
                db.session.commit()
        else:
            LOG.debug(
                "create contact for alias %s and email %s, header %s",
                alias,
                contact_email,
                header,
            )

            try:
                contact = Contact.create(
                    user_id=alias.user_id,
                    alias_id=alias.id,
                    website_email=contact_email,
                    name=contact_name,
                    reply_email=generate_reply_email(contact_email, alias.user),
                    is_cc=header.lower() == "cc",
                    from_header=addr,
                )
                db.session.commit()
            except IntegrityError:
                LOG.warning("Contact %s %s already exist", alias, contact_email)
                db.session.rollback()
                contact = Contact.get_by(alias_id=alias.id, website_email=contact_email)

        new_addrs.append(contact.new_addr())

    if new_addrs:
        new_header = ",".join(new_addrs)
        LOG.d("Replace %s header, old: %s, new: %s", header, msg[header], new_header)
        add_or_replace_header(msg, header, new_header)
    else:
        LOG.d("Delete %s header, old value %s", header, msg[header])
        delete_header(msg, header)


def replace_header_when_reply(msg: Message, alias: Alias, header: str):
    """
    Replace CC or To Reply emails by original emails
    """
    new_addrs: [str] = []

    for _, reply_email in getaddresses(msg.get_all(header, [])):
        # no transformation when alias is already in the header
        if reply_email == alias.email:
            continue

        contact = Contact.get_by(reply_email=reply_email)
        if not contact:
            LOG.warning(
                "%s email in reply phase %s must be reply emails", header, reply_email
            )
            # still keep this email in header
            new_addrs.append(reply_email)
        else:
            new_addrs.append(formataddr((contact.name, contact.website_email)))

    if new_addrs:
        new_header = ",".join(new_addrs)
        LOG.d("Replace %s header, old: %s, new: %s", header, msg[header], new_header)
        add_or_replace_header(msg, header, new_header)
    else:
        LOG.d("delete the %s header. Old value %s", header, msg[header])
        delete_header(msg, header)


def prepare_pgp_message(
    orig_msg: Message, pgp_fingerprint: str, public_key: str, can_sign: bool = False
) -> Message:
    msg = MIMEMultipart("encrypted", protocol="application/pgp-encrypted")

    # clone orig message to avoid modifying it
    clone_msg = copy(orig_msg)

    # copy all headers from original message except all standard MIME headers
    for i in reversed(range(len(clone_msg._headers))):
        header_name = clone_msg._headers[i][0].lower()
        if header_name.lower() not in _MIME_HEADERS:
            msg[header_name] = clone_msg._headers[i][1]

    # Delete unnecessary headers in clone_msg except _MIME_HEADERS to save space
    delete_all_headers_except(
        clone_msg,
        _MIME_HEADERS,
    )

    if clone_msg["Content-Type"] is None:
        LOG.d("Content-Type missing")
        clone_msg["Content-Type"] = "text/plain"

    if clone_msg["Mime-Version"] is None:
        LOG.d("Mime-Version missing")
        clone_msg["Mime-Version"] = "1.0"

    first = MIMEApplication(
        _subtype="pgp-encrypted", _encoder=encoders.encode_7or8bit, _data=""
    )
    first.set_payload("Version: 1")
    msg.attach(first)

    if can_sign and PGP_SENDER_PRIVATE_KEY:
        LOG.d("Sign msg")
        clone_msg = sign_msg(clone_msg)

    # use pgpy as fallback
    second = MIMEApplication(
        "octet-stream", _encoder=encoders.encode_7or8bit, name="encrypted.asc"
    )
    second.add_header("Content-Disposition", 'inline; filename="encrypted.asc"')

    # encrypt
    # use pgpy as fallback
    msg_bytes = to_bytes(clone_msg)
    try:
        encrypted_data = pgp_utils.encrypt_file(BytesIO(msg_bytes), pgp_fingerprint)
        second.set_payload(encrypted_data)
    except PGPException:
        LOG.warning("Cannot encrypt using python-gnupg, use pgpy")
        encrypted = pgp_utils.encrypt_file_with_pgpy(msg_bytes, public_key)
        second.set_payload(str(encrypted))

    msg.attach(second)

    return msg


def sign_msg(msg: Message) -> Message:
    container = MIMEMultipart(
        "signed", protocol="application/pgp-signature", micalg="pgp-sha256"
    )
    container.attach(msg)

    signature = MIMEApplication(
        _subtype="pgp-signature", name="signature.asc", _data="", _encoder=encode_noop
    )
    signature.add_header("Content-Disposition", 'attachment; filename="signature.asc"')

    try:
        signature.set_payload(sign_data(to_bytes(msg).replace(b"\n", b"\r\n")))
    except Exception:
        LOG.exception("Cannot sign, try using pgpy")
        signature.set_payload(
            sign_data_with_pgpy(to_bytes(msg).replace(b"\n", b"\r\n"))
        )

    container.attach(signature)

    return container


def handle_email_sent_to_ourself(alias, mailbox, msg: Message, user):
    # store the refused email
    random_name = str(uuid.uuid4())
    full_report_path = f"refused-emails/cycle-{random_name}.eml"
    s3.upload_email_from_bytesio(full_report_path, BytesIO(to_bytes(msg)), random_name)
    refused_email = RefusedEmail.create(
        path=None, full_report_path=full_report_path, user_id=alias.user_id
    )
    db.session.commit()
    LOG.d("Create refused email %s", refused_email)
    # link available for 6 days as it gets deleted in 7 days
    refused_email_url = refused_email.get_url(expires_in=518400)

    send_email_at_most_times(
        user,
        ALERT_SEND_EMAIL_CYCLE,
        mailbox.email,
        f"Email sent to {alias.email} from its own mailbox {mailbox.email}",
        render(
            "transactional/cycle-email.txt",
            alias=alias,
            mailbox=mailbox,
            refused_email_url=refused_email_url,
        ),
        render(
            "transactional/cycle-email.html",
            alias=alias,
            mailbox=mailbox,
            refused_email_url=refused_email_url,
        ),
    )


def handle_forward(envelope, msg: Message, rcpt_to: str) -> List[Tuple[bool, str]]:
    """return an array of SMTP status (is_success, smtp_status)
    is_success indicates whether an email has been delivered and
    smtp_status is the SMTP Status ("250 Message accepted", "550 Non-existent email address", etc)
    """
    address = rcpt_to  # alias@SL

    alias = Alias.get_by(email=address)
    if not alias:
        LOG.d("alias %s not exist. Try to see if it can be created on the fly", address)
        alias = try_auto_create(address)
        if not alias:
            LOG.d("alias %s cannot be created on-the-fly, return 550", address)
            return [(False, "550 SL E3 Email not exist")]

    user = alias.user

    if user.disabled:
        LOG.warning("User %s disabled, disable forwarding emails for %s", user, alias)
        return [(False, "550 SL E20 Account disabled")]

    mail_from = envelope.mail_from
    for mb in alias.mailboxes:
        # email send from a mailbox to alias
        if mb.email == mail_from:
            LOG.warning("cycle email sent from %s to %s", mb, alias)
            handle_email_sent_to_ourself(alias, mb, msg, user)
            return [(True, "250 Message accepted for delivery")]

    LOG.d("Create or get contact for %s %s", msg["From"], msg["Reply-To"])
    # prefer using Reply-To when creating contact
    if msg["Reply-To"]:
        # force convert header to string, sometimes contact_from_header is Header object
        LOG.d("Use Reply-To header")
        from_header = str(msg["Reply-To"])
    else:
        from_header = str(msg["From"])

    contact = get_or_create_contact(from_header, envelope.mail_from, alias)

    if not alias.enabled:
        LOG.d("%s is disabled, do not forward", alias)
        EmailLog.create(
            contact_id=contact.id, user_id=contact.user_id, blocked=True, commit=True
        )
        db.session.commit()
        # do not return 5** to allow user to receive emails later when alias is enabled
        return [(True, "250 Message accepted for delivery")]

    ret = []
    mailboxes = alias.mailboxes

    # no valid mailbox
    if not mailboxes:
        return [(False, "550 SL E16 invalid mailbox")]

    # no need to create a copy of message
    for mailbox in mailboxes:
        if not mailbox.verified:
            LOG.debug("Mailbox %s unverified, do not forward", mailbox)
            ret.append((False, "550 SL E19 unverified mailbox"))
        else:
            # create a copy of message for each forward
            ret.append(
                forward_email_to_mailbox(
                    alias,
                    copy(msg),
                    contact,
                    envelope,
                    mailbox,
                    user,
                )
            )

    return ret


def forward_email_to_mailbox(
    alias,
    msg: Message,
    contact: Contact,
    envelope,
    mailbox,
    user,
) -> (bool, str):
    LOG.d("Forward %s -> %s -> %s", contact, alias, mailbox)

    if mailbox.disabled:
        LOG.debug("%s disabled, do not forward")
        return False, "550 SL E21 Disabled mailbox"

    # sanity check: make sure mailbox is not actually an alias
    if get_email_domain_part(alias.email) == get_email_domain_part(mailbox.email):
        LOG.warning(
            "Mailbox has the same domain as alias. %s -> %s -> %s",
            contact,
            alias,
            mailbox,
        )
        mailbox_url = f"{URL}/dashboard/mailbox/{mailbox.id}/"
        send_email_with_rate_control(
            user,
            ALERT_MAILBOX_IS_ALIAS,
            user.email,
            f"Your mailbox {mailbox.email} and alias {alias.email} use the same domain",
            render(
                "transactional/mailbox-invalid.txt",
                mailbox=mailbox,
                mailbox_url=mailbox_url,
                alias=alias,
            ),
            render(
                "transactional/mailbox-invalid.html",
                mailbox=mailbox,
                mailbox_url=mailbox_url,
                alias=alias,
            ),
            max_nb_alert=1,
        )

        # retry later
        # so when user fixes the mailbox, the email can be delivered
        return False, "421 SL E14"

    email_log = EmailLog.create(
        contact_id=contact.id, user_id=user.id, mailbox_id=mailbox.id, commit=True
    )

    # Spam check
    spam_status = ""
    is_spam = False

    if SPAMASSASSIN_HOST:
        start = time.time()
        spam_score, spam_report = get_spam_score(msg, email_log)
        LOG.d(
            "%s -> %s - spam score %s in %s seconds. Spam report %s",
            contact,
            alias,
            spam_score,
            time.time() - start,
            spam_report,
        )
        email_log.spam_score = spam_score
        db.session.commit()

        if (user.max_spam_score and spam_score > user.max_spam_score) or (
            not user.max_spam_score and spam_score > MAX_SPAM_SCORE
        ):
            is_spam = True
            # only set the spam report for spam
            email_log.spam_report = spam_report
    else:
        is_spam, spam_status = get_spam_info(msg, max_score=user.max_spam_score)

    if is_spam:
        LOG.warning(
            "Email detected as spam. %s -> %s. Spam Score: %s, Spam Report: %s",
            contact,
            alias,
            email_log.spam_score,
            email_log.spam_report,
        )
        email_log.is_spam = True
        email_log.spam_status = spam_status
        db.session.commit()

        handle_spam(contact, alias, msg, user, mailbox, email_log)
        return False, "550 SL E1 Email detected as spam"

    if contact.invalid_email:
        LOG.d("add noreply information %s %s", alias, mailbox)
        msg = add_header(
            msg,
            f"""Email sent to {alias.email} from an invalid address and cannot be replied""",
            f"""Email sent to {alias.email} from an invalid address and cannot be replied""",
        )

    # create PGP email if needed
    if mailbox.pgp_enabled() and user.is_premium() and not alias.disable_pgp:
        LOG.d("Encrypt message using mailbox %s", mailbox)
        if mailbox.generic_subject:
            LOG.d("Use a generic subject for %s", mailbox)
            orig_subject = msg["Subject"]
            orig_subject = get_header_unicode(orig_subject)
            add_or_replace_header(msg, "Subject", mailbox.generic_subject)
            msg = add_header(
                msg,
                f"""Forwarded by SimpleLogin to {alias.email} with "{orig_subject}" as subject""",
                f"""Forwarded by SimpleLogin to {alias.email} with <b>{orig_subject}</b> as subject""",
            )

        try:
            msg = prepare_pgp_message(
                msg, mailbox.pgp_finger_print, mailbox.pgp_public_key, can_sign=True
            )
        except PGPException:
            LOG.exception(
                "Cannot encrypt message %s -> %s. %s %s", contact, alias, mailbox, user
            )
            # so the client can retry later
            return False, "421 SL E12 Retry later"

    # add custom header
    add_or_replace_header(msg, _DIRECTION, "Forward")

    # remove reply-to & sender header if present
    delete_header(msg, "Reply-To")
    delete_header(msg, "Sender")

    delete_header(msg, _IP_HEADER)
    add_or_replace_header(msg, _EMAIL_LOG_ID_HEADER, str(email_log.id))

    # fill up the message-id if ever it's absent. Should never happen for a normal email
    if not msg["Message-ID"]:
        LOG.info("Set Message-ID before forwarding email")
        msg["Message-ID"] = make_msgid(str(email_log.id), EMAIL_DOMAIN)

    add_or_replace_header(msg, _ENVELOPE_FROM, envelope.mail_from)

    if not msg["Date"]:
        date_header = formatdate()
        msg["Date"] = date_header

    # change the from header so the sender comes from a reverse-alias
    # so it can pass DMARC check
    # replace the email part in from: header
    contact_from_header = msg["From"]
    new_from_header = contact.new_addr()
    add_or_replace_header(msg, "From", new_from_header)
    LOG.d("new_from_header:%s, old header %s", new_from_header, contact_from_header)

    # replace CC & To emails by reverse-alias for all emails that are not alias
    replace_header_when_forward(msg, alias, "Cc")
    replace_header_when_forward(msg, alias, "To")

    # add List-Unsubscribe header
    unsubscribe_link, via_email = alias.unsubscribe_link()
    add_or_replace_header(msg, "List-Unsubscribe", f"<{unsubscribe_link}>")
    if not via_email:
        add_or_replace_header(
            msg, "List-Unsubscribe-Post", "List-Unsubscribe=One-Click"
        )

    add_dkim_signature(msg, EMAIL_DOMAIN)

    LOG.d(
        "Forward mail from %s to %s, mail_options %s, rcpt_options %s ",
        contact.website_email,
        mailbox.email,
        envelope.mail_options,
        envelope.rcpt_options,
    )

    try:
        sl_sendmail(
            # use a different envelope sender for each forward (aka VERP)
            BOUNCE_EMAIL.format(email_log.id),
            mailbox.email,
            msg,
            envelope.mail_options,
            envelope.rcpt_options,
        )
    except SMTPRecipientsRefused:
        # that means the mailbox is maybe invalid
        LOG.warning(
            "SMTPRecipientsRefused forward phase %s -> %s -> %s",
            contact,
            alias,
            mailbox,
        )
        # return 421 so Postfix can retry later
        return False, "421 SL E17 Retry later"
    else:
        db.session.commit()
        return True, "250 Message accepted for delivery"


def handle_reply(envelope, msg: Message, rcpt_to: str) -> (bool, str):
    """
    return whether an email has been delivered and
    the smtp status ("250 Message accepted", "550 Non-existent email address", etc)
    """
    reply_email = rcpt_to

    # reply_email must end with EMAIL_DOMAIN
    if not reply_email.endswith(EMAIL_DOMAIN):
        LOG.warning(f"Reply email {reply_email} has wrong domain")
        return False, "550 SL E2"

    # handle case where reply email is generated with non-allowed char
    reply_email = normalize_reply_email(reply_email)

    contact = Contact.get_by(reply_email=reply_email)
    if not contact:
        LOG.warning(f"No such forward-email with {reply_email} as reply-email")
        return False, "550 SL E4 Email not exist"

    alias = contact.alias
    address: str = contact.alias.email
    alias_domain = address[address.find("@") + 1 :]

    # Sanity check: verify alias domain is managed by SimpleLogin
    # scenario: a user have removed a domain but due to a bug, the aliases are still there
    if not is_valid_alias_address_domain(alias.email):
        LOG.exception("%s domain isn't known", alias)
        return False, "550 SL E5"

    user = alias.user
    mail_from = envelope.mail_from

    if user.disabled:
        LOG.exception(
            "User %s disabled, disable sending emails from %s to %s",
            user,
            alias,
            contact,
        )
        return [(False, "550 SL E20 Account disabled")]

    # bounce email initiated by Postfix
    # can happen in case emails cannot be delivered to user-email
    # in this case Postfix will try to send a bounce report to original sender, which is
    # the "reply email"
    if mail_from == "<>":
        LOG.warning(
            "Bounce when sending to alias %s from %s, user %s",
            alias,
            contact,
            user,
        )

        handle_bounce_deprecated(contact, alias, msg, user)
        return False, "550 SL E6"

    # Anti-spoofing
    mailbox = get_mailbox_from_mail_from(mail_from, alias)
    if not mailbox:
        if alias.disable_email_spoofing_check:
            # ignore this error, use default alias mailbox
            LOG.warning(
                "ignore unknown sender to reverse-alias %s: %s -> %s",
                mail_from,
                alias,
                contact,
            )
            mailbox = alias.mailbox
        else:
            # only mailbox can send email to the reply-email
            handle_unknown_mailbox(envelope, msg, reply_email, user, alias, contact)
            return False, "550 SL E7"

    if ENFORCE_SPF and mailbox.force_spf and not alias.disable_email_spoofing_check:
        ip = msg[_IP_HEADER]
        if not spf_pass(ip, envelope, mailbox, user, alias, contact.website_email, msg):
            # cannot use 4** here as sender will retry. 5** because that generates bounce report
            return True, "250 SL E11"

    email_log = EmailLog.create(
        contact_id=contact.id,
        is_reply=True,
        user_id=contact.user_id,
        mailbox_id=mailbox.id,
        commit=True,
    )

    # Spam check
    spam_status = ""
    is_spam = False

    # do not use user.max_spam_score here
    if SPAMASSASSIN_HOST:
        start = time.time()
        spam_score, spam_report = get_spam_score(msg, email_log)
        LOG.d(
            "%s -> %s - spam score %s in %s seconds. Spam report %s",
            alias,
            contact,
            spam_score,
            time.time() - start,
            spam_report,
        )
        email_log.spam_score = spam_score
        if spam_score > MAX_REPLY_PHASE_SPAM_SCORE:
            is_spam = True
            # only set the spam report for spam
            email_log.spam_report = spam_report
    else:
        is_spam, spam_status = get_spam_info(msg, max_score=MAX_REPLY_PHASE_SPAM_SCORE)

    if is_spam:
        LOG.warning(
            "Email detected as spam. Reply phase. %s -> %s. Spam Score: %s, Spam Report: %s",
            alias,
            contact,
            email_log.spam_score,
            email_log.spam_report,
        )

        email_log.is_spam = True
        email_log.spam_status = spam_status
        db.session.commit()

        handle_spam(contact, alias, msg, user, mailbox, email_log, is_reply=True)
        return False, "550 SL E15 Email detected as spam"

    delete_all_headers_except(
        msg,
        [
            "From",
            "To",
            "Cc",
            "Subject",
            # References and In-Reply-To are used for keeping the email thread
            "References",
            "In-Reply-To",
        ]
        + _MIME_HEADERS,
    )

    # replace the reverse-alias (i.e. "ra+string@simplelogin.co") by the contact email in the email body
    # as this is usually included when replying
    if user.replace_reverse_alias:
        LOG.d("Replace reverse-alias %s by contact email %s", reply_email, contact)
        msg = replace(msg, reply_email, contact.website_email)

    # create PGP email if needed
    if contact.pgp_finger_print and user.is_premium():
        LOG.d("Encrypt message for contact %s", contact)
        try:
            msg = prepare_pgp_message(
                msg, contact.pgp_finger_print, contact.pgp_public_key
            )
        except PGPException:
            LOG.exception(
                "Cannot encrypt message %s -> %s. %s %s", alias, contact, mailbox, user
            )
            # to not save the email_log
            EmailLog.delete(email_log.id)
            db.session.commit()
            # return 421 so the client can retry later
            return False, "421 SL E13 Retry later"

    db.session.commit()

    # make the email comes from alias
    from_header = alias.email
    # add alias name from alias
    if alias.name:
        LOG.d("Put alias name in from header")
        from_header = formataddr((alias.name, alias.email))
    elif alias.custom_domain:
        LOG.d("Put domain default alias name in from header")

        # add alias name from domain
        if alias.custom_domain.name:
            from_header = formataddr((alias.custom_domain.name, alias.email))

    add_or_replace_header(msg, "From", from_header)

    replace_header_when_reply(msg, alias, "To")
    replace_header_when_reply(msg, alias, "Cc")

    # Message-ID can reveal about the mailbox -> replace it
    add_or_replace_header(
        msg,
        "Message-ID",
        make_msgid(str(email_log.id), get_email_domain_part(alias.email)),
    )
    date_header = formatdate()
    msg["Date"] = date_header

    msg[_DIRECTION] = "Reply"
    msg[_EMAIL_LOG_ID_HEADER] = str(email_log.id)

    LOG.d(
        "send email from %s to %s, mail_options:%s,rcpt_options:%s",
        alias.email,
        contact.website_email,
        envelope.mail_options,
        envelope.rcpt_options,
    )

    if should_add_dkim_signature(alias_domain):
        add_dkim_signature(msg, alias_domain)

    try:
        sl_sendmail(
            # VERP
            BOUNCE_EMAIL.format(email_log.id),
            contact.website_email,
            msg,
            envelope.mail_options,
            envelope.rcpt_options,
        )
    except Exception:
        # to not save the email_log
        db.session.rollback()

        LOG.warning("Cannot send email from %s to %s", alias, contact)
        send_email(
            mailbox.email,
            f"Email cannot be sent to {contact.email} from {alias.email}",
            render(
                "transactional/reply-error.txt",
                user=user,
                alias=alias,
                contact=contact,
                contact_domain=get_email_domain_part(contact.email),
            ),
            render(
                "transactional/reply-error.html",
                user=user,
                alias=alias,
                contact=contact,
                contact_domain=get_email_domain_part(contact.email),
            ),
        )

    # return 250 even if error as user is already informed of the incident and can retry sending the email

    return True, "250 Message accepted for delivery"


def get_mailbox_from_mail_from(mail_from: str, alias) -> Optional[Mailbox]:
    """return the corresponding mailbox given the mail_from and alias
    Usually the mail_from=mailbox.email but it can also be one of the authorized address
    """
    for mailbox in alias.mailboxes:
        if mailbox.email == mail_from:
            return mailbox

        for address in mailbox.authorized_addresses:
            if address.email == mail_from:
                LOG.debug(
                    "Found an authorized address for %s %s %s", alias, mailbox, address
                )
                return mailbox

    return None


def spf_pass(
    ip: str,
    envelope,
    mailbox: Mailbox,
    user: User,
    alias: Alias,
    contact_email: str,
    msg: Message,
) -> bool:
    if ip:
        LOG.d("Enforce SPF")
        try:
            r = spf.check2(i=ip, s=envelope.mail_from, h=None)
        except Exception:
            LOG.exception("SPF error, mailbox %s, ip %s", mailbox.email, ip)
        else:
            # TODO: Handle temperr case (e.g. dns timeout)
            # only an absolute pass, or no SPF policy at all is 'valid'
            if r[0] not in ["pass", "none"]:
                LOG.warning(
                    "SPF fail for mailbox %s, reason %s, failed IP %s",
                    mailbox.email,
                    r[0],
                    ip,
                )
                subject = get_header_unicode(msg["Subject"])
                send_email_with_rate_control(
                    user,
                    ALERT_SPF,
                    mailbox.email,
                    f"SimpleLogin Alert: attempt to send emails from your alias {alias.email} from unknown IP Address",
                    render(
                        "transactional/spf-fail.txt",
                        alias=alias.email,
                        ip=ip,
                        mailbox_url=URL + f"/dashboard/mailbox/{mailbox.id}#spf",
                        to_email=contact_email,
                        subject=subject,
                        time=arrow.now(),
                    ),
                    render(
                        "transactional/spf-fail.html",
                        ip=ip,
                        mailbox_url=URL + f"/dashboard/mailbox/{mailbox.id}#spf",
                        to_email=contact_email,
                        subject=subject,
                        time=arrow.now(),
                    ),
                )
                return False

    else:
        LOG.warning(
            "Could not find %s header %s -> %s",
            _IP_HEADER,
            mailbox.email,
            contact_email,
        )

    return True


def handle_unknown_mailbox(
    envelope, msg, reply_email: str, user: User, alias: Alias, contact: Contact
):
    LOG.warning(
        "Reply email can only be used by mailbox. "
        "Actual mail_from: %s. msg from header: %s, reverse-alias %s, %s %s %s",
        envelope.mail_from,
        msg["From"],
        reply_email,
        alias,
        user,
        contact,
    )

    authorize_address_link = (
        f"{URL}/dashboard/mailbox/{alias.mailbox_id}/#authorized-address"
    )
    mailbox_emails = [mailbox.email for mailbox in alias.mailboxes]
    send_email_with_rate_control(
        user,
        ALERT_REVERSE_ALIAS_UNKNOWN_MAILBOX,
        user.email,
        f"Attempt to use your alias {alias.email} from {envelope.mail_from}",
        render(
            "transactional/reply-must-use-personal-email.txt",
            alias=alias,
            sender=envelope.mail_from,
            authorize_address_link=authorize_address_link,
            mailbox_emails=mailbox_emails,
        ),
        render(
            "transactional/reply-must-use-personal-email.html",
            alias=alias,
            sender=envelope.mail_from,
            authorize_address_link=authorize_address_link,
            mailbox_emails=mailbox_emails,
        ),
    )

    # Notify sender that they cannot send emails to this address
    send_email_with_rate_control(
        user,
        ALERT_REVERSE_ALIAS_UNKNOWN_MAILBOX,
        envelope.mail_from,
        f"Your email ({envelope.mail_from}) is not allowed to send emails to {reply_email}",
        render(
            "transactional/send-from-alias-from-unknown-sender.txt",
            sender=envelope.mail_from,
            reply_email=reply_email,
        ),
        render(
            "transactional/send-from-alias-from-unknown-sender.html",
            sender=envelope.mail_from,
            reply_email=reply_email,
        ),
    )


def handle_bounce_deprecated(contact: Contact, alias: Alias, msg: Message, user: User):
    """
    Handle bounce that is sent to the reverse-alias
    Happens when  an email cannot be forwarded to a mailbox
    """
    disable_alias_link = f"{URL}/dashboard/unsubscribe/{alias.id}"

    # Store the bounced email
    # generate a name for the email
    random_name = str(uuid.uuid4())

    full_report_path = f"refused-emails/full-{random_name}.eml"
    s3.upload_email_from_bytesio(full_report_path, BytesIO(to_bytes(msg)), random_name)

    file_path = None

    orig_msg = get_orig_message_from_bounce(msg)
    if not orig_msg:
        # Some MTA does not return the original message in bounce message
        # nothing we can do here
        LOG.warning(
            "Cannot parse original message from bounce message %s %s %s %s",
            alias,
            user,
            contact,
            full_report_path,
        )
    else:
        file_path = f"refused-emails/{random_name}.eml"
        s3.upload_email_from_bytesio(file_path, BytesIO(to_bytes(msg)), random_name)

    refused_email = RefusedEmail.create(
        path=file_path, full_report_path=full_report_path, user_id=user.id
    )
    db.session.flush()
    LOG.d("Create refused email %s", refused_email)

    # try to parse email_log
    email_log = None
    try:
        email_log_id = int(get_header_from_bounce(msg, _EMAIL_LOG_ID_HEADER))
    except Exception:
        LOG.warning("cannot get email log id from bounce report, %s", refused_email)
    else:
        email_log = EmailLog.get(email_log_id)

    # create new email_log if unable to parse from bounce report
    if not email_log:
        LOG.warning("cannot get the original email_log, create a new one")
        email_log: EmailLog = EmailLog.create(
            contact_id=contact.id, user_id=contact.user_id
        )

    # try to get mailbox
    mailbox = None
    if email_log:
        mailbox = email_log.mailbox

    # use the default mailbox if unable to parse from bounce report
    if not mailbox:
        LOG.warning("Use %s default mailbox %s", alias, refused_email)
        mailbox = alias.mailbox

    email_log.bounced = True
    email_log.refused_email_id = refused_email.id
    email_log.bounced_mailbox_id = mailbox.id
    db.session.commit()

    refused_email_url = f"{URL}/dashboard/refused_email?highlight_id={email_log.id}"

    nb_bounced = EmailLog.filter_by(contact_id=contact.id, bounced=True).count()
    if nb_bounced >= 2 and alias.cannot_be_disabled:
        LOG.warning("%s cannot be disabled", alias)

    # inform user of this bounce
    if not should_disable(alias):
        LOG.d(
            "Inform user %s about a bounce from contact %s to alias %s",
            user,
            contact,
            alias,
        )
        send_email_with_rate_control(
            user,
            ALERT_BOUNCE_EMAIL,
            user.email,
            f"Email from {contact.website_email} to {alias.email} cannot be delivered to your inbox",
            render(
                "transactional/bounce/bounced-email.txt",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            render(
                "transactional/bounce/bounced-email.html",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            max_nb_alert=10,
        )
    else:
        LOG.warning(
            "Disable alias %s now",
            alias,
        )
        alias.enabled = False
        db.session.commit()

        send_email_with_rate_control(
            user,
            ALERT_BOUNCE_EMAIL,
            user.email,
            f"Alias {alias.email} has been disabled due to second undelivered email from {contact.website_email}",
            render(
                "transactional/bounce/automatic-disable-alias.txt",
                alias=alias,
                website_email=contact.website_email,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            render(
                "transactional/bounce/automatic-disable-alias.html",
                alias=alias,
                website_email=contact.website_email,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            max_nb_alert=10,
        )


def handle_bounce_forward_phase(msg: Message, email_log: EmailLog):
    """
    Handle forward phase bounce
    Happens when an email cannot be sent to a mailbox
    """
    contact = email_log.contact
    alias = contact.alias
    user = alias.user
    mailbox = email_log.mailbox

    # email_log.mailbox should be set during the forward phase
    if not mailbox:
        LOG.exception("Use %s default mailbox %s", alias, alias.mailbox)
        mailbox = alias.mailbox

    LOG.debug(
        "Handle forward bounce %s -> %s -> %s. %s", contact, alias, mailbox, email_log
    )

    # Store the bounced email, generate a name for the email
    random_name = str(uuid.uuid4())

    full_report_path = f"refused-emails/full-{random_name}.eml"
    s3.upload_email_from_bytesio(
        full_report_path, BytesIO(to_bytes(msg)), f"full-{random_name}"
    )

    file_path = None

    orig_msg = get_orig_message_from_bounce(msg)
    if not orig_msg:
        # Some MTA does not return the original message in bounce message
        # nothing we can do here
        LOG.warning(
            "Cannot parse original message from bounce message %s %s %s %s",
            alias,
            user,
            contact,
            full_report_path,
        )
    else:
        file_path = f"refused-emails/{random_name}.eml"
        s3.upload_email_from_bytesio(
            file_path, BytesIO(to_bytes(orig_msg)), random_name
        )

    refused_email = RefusedEmail.create(
        path=file_path, full_report_path=full_report_path, user_id=user.id
    )
    db.session.flush()
    LOG.d("Create refused email %s", refused_email)

    email_log.bounced = True
    email_log.refused_email_id = refused_email.id
    email_log.bounced_mailbox_id = mailbox.id
    db.session.commit()

    refused_email_url = f"{URL}/dashboard/refused_email?highlight_id={email_log.id}"

    nb_bounced = EmailLog.filter_by(contact_id=contact.id, bounced=True).count()
    if nb_bounced >= 2 and alias.cannot_be_disabled:
        LOG.warning("%s cannot be disabled", alias)

    # inform user of this bounce
    if not should_disable(alias):
        LOG.d(
            "Inform user %s about a bounce from contact %s to alias %s",
            user,
            contact,
            alias,
        )
        disable_alias_link = f"{URL}/dashboard/unsubscribe/{alias.id}"
        send_email_with_rate_control(
            user,
            ALERT_BOUNCE_EMAIL,
            user.email,
            f"Email from {contact.website_email} to {alias.email} cannot be delivered to your mailbox",
            render(
                "transactional/bounce/bounced-email.txt",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            render(
                "transactional/bounce/bounced-email.html",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            max_nb_alert=10,
        )
    else:
        LOG.warning(
            "Disable alias %s now",
            alias,
        )
        alias.enabled = False
        db.session.commit()

        send_email_with_rate_control(
            user,
            ALERT_BOUNCE_EMAIL,
            user.email,
            f"Alias {alias.email} has been disabled due to second undelivered email from {contact.website_email}",
            render(
                "transactional/bounce/automatic-disable-alias.txt",
                alias=alias,
                website_email=contact.website_email,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            render(
                "transactional/bounce/automatic-disable-alias.html",
                alias=alias,
                website_email=contact.website_email,
                refused_email_url=refused_email_url,
                mailbox_email=mailbox.email,
            ),
            max_nb_alert=10,
        )


def handle_bounce_reply_phase(msg: Message, email_log: EmailLog):
    """
    Handle reply phase bounce
    Happens when  an email cannot be sent from an alias to a contact
    """
    contact = email_log.contact
    alias = contact.alias
    user = alias.user
    mailbox = email_log.mailbox or alias.mailbox

    LOG.debug(
        "Handle reply bounce %s -> %s -> %s.%s", mailbox, alias, contact, email_log
    )

    # Store the bounced email
    # generate a name for the email
    random_name = str(uuid.uuid4())

    full_report_path = f"refused-emails/full-{random_name}.eml"
    s3.upload_email_from_bytesio(full_report_path, BytesIO(to_bytes(msg)), random_name)

    orig_msg = get_orig_message_from_bounce(msg)
    file_path = None
    if orig_msg:
        file_path = f"refused-emails/{random_name}.eml"
        s3.upload_email_from_bytesio(
            file_path, BytesIO(to_bytes(orig_msg)), random_name
        )

    refused_email = RefusedEmail.create(
        path=file_path, full_report_path=full_report_path, user_id=user.id, commit=True
    )
    LOG.d("Create refused email %s", refused_email)

    email_log.bounced = True
    email_log.refused_email_id = refused_email.id

    email_log.bounced_mailbox_id = mailbox.id

    db.session.commit()

    refused_email_url = f"{URL}/dashboard/refused_email?highlight_id={email_log.id}"

    LOG.d(
        "Inform user %s about bounced email sent by %s to %s",
        user,
        alias,
        contact,
    )
    send_email_with_rate_control(
        user,
        ALERT_BOUNCE_EMAIL_REPLY_PHASE,
        mailbox.email,
        f"Email cannot be sent to { contact.email } from your alias { alias.email }",
        render(
            "transactional/bounce/bounce-email-reply-phase.txt",
            alias=alias,
            contact=contact,
            refused_email_url=refused_email_url,
        ),
        render(
            "transactional/bounce/bounce-email-reply-phase.html",
            alias=alias,
            contact=contact,
            refused_email_url=refused_email_url,
        ),
    )


def handle_spam(
    contact: Contact,
    alias: Alias,
    msg: Message,
    user: User,
    mailbox: Mailbox,
    email_log: EmailLog,
    is_reply=False,  # whether the email is in forward or reply phase
):
    # Store the report & original email
    orig_msg = get_orig_message_from_spamassassin_report(msg)
    # generate a name for the email
    random_name = str(uuid.uuid4())

    full_report_path = f"spams/full-{random_name}.eml"
    s3.upload_email_from_bytesio(full_report_path, BytesIO(to_bytes(msg)), random_name)

    file_path = None
    if orig_msg:
        file_path = f"spams/{random_name}.eml"
        s3.upload_email_from_bytesio(
            file_path, BytesIO(to_bytes(orig_msg)), random_name
        )

    refused_email = RefusedEmail.create(
        path=file_path, full_report_path=full_report_path, user_id=user.id
    )
    db.session.flush()

    email_log.refused_email_id = refused_email.id
    db.session.commit()

    LOG.d("Create spam email %s", refused_email)

    refused_email_url = f"{URL}/dashboard/refused_email?highlight_id={email_log.id}"
    disable_alias_link = f"{URL}/dashboard/unsubscribe/{alias.id}"

    if is_reply:
        LOG.d(
            "Inform %s (%s) about spam email sent from alias %s to %s. %s",
            mailbox,
            user,
            alias,
            contact,
            refused_email,
        )
        send_email_with_rate_control(
            user,
            ALERT_SPAM_EMAIL,
            mailbox.email,
            f"Email from {alias.email} to {contact.website_email} is detected as spam",
            render(
                "transactional/spam-email-reply-phase.txt",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
            ),
            render(
                "transactional/spam-email-reply-phase.html",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
            ),
        )
    else:
        # inform user
        LOG.d(
            "Inform %s (%s) about spam email sent by %s to alias %s",
            mailbox,
            user,
            contact,
            alias,
        )
        send_email_with_rate_control(
            user,
            ALERT_SPAM_EMAIL,
            mailbox.email,
            f"Email from {contact.website_email} to {alias.email} is detected as spam",
            render(
                "transactional/spam-email.txt",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
            ),
            render(
                "transactional/spam-email.html",
                alias=alias,
                website_email=contact.website_email,
                disable_alias_link=disable_alias_link,
                refused_email_url=refused_email_url,
            ),
        )


def handle_unsubscribe(envelope: Envelope) -> str:
    """return the SMTP status"""
    msg = email.message_from_bytes(envelope.original_content)

    # format: alias_id:
    subject = msg["Subject"]
    try:
        # subject has the format {alias.id}=
        if subject.endswith("="):
            alias_id = int(subject[:-1])
        # {user.id}*
        elif subject.endswith("*"):
            user_id = int(subject[:-1])
            return handle_unsubscribe_user(user_id, envelope.mail_from)
        # some email providers might strip off the = suffix
        else:
            alias_id = int(subject)

        alias = Alias.get(alias_id)
    except Exception:
        LOG.warning("Cannot parse alias from subject %s", msg["Subject"])
        return "550 SL E8 Wrongly formatted subject"

    if not alias:
        LOG.warning("No such alias %s", alias_id)
        return "550 SL E9 Email not exist"

    # This sender cannot unsubscribe
    mail_from = envelope.mail_from
    # Only alias's owning mailbox can send the unsubscribe request
    mailbox = get_mailbox_from_mail_from(mail_from, alias)
    if not mailbox:
        LOG.d("%s cannot disable alias %s", envelope.mail_from, alias)
        return "550 SL E10 unauthorized"

    # Sender is owner of this alias
    alias.enabled = False
    db.session.commit()
    user = alias.user

    enable_alias_url = URL + f"/dashboard/?highlight_alias_id={alias.id}"
    for mailbox in alias.mailboxes:
        send_email(
            mailbox.email,
            f"Alias {alias.email} has been disabled successfully",
            render(
                "transactional/unsubscribe-disable-alias.txt",
                user=user,
                alias=alias.email,
                enable_alias_url=enable_alias_url,
            ),
            render(
                "transactional/unsubscribe-disable-alias.html",
                user=user,
                alias=alias.email,
                enable_alias_url=enable_alias_url,
            ),
        )

    return "250 Unsubscribe request accepted"


def handle_unsubscribe_user(user_id: int, mail_from: str) -> str:
    """return the SMTP status"""
    user = User.get(user_id)
    if not user:
        LOG.exception("No such user %s %s", user_id, mail_from)
        return "550 SL E22 so such user"

    if mail_from != user.email:
        LOG.exception("Unauthorized mail_from %s %s", user, mail_from)
        return "550 SL E23 unsubscribe error"

    user.notification = False
    db.session.commit()

    send_email(
        user.email,
        "You have been unsubscribed from SimpleLogin newsletter",
        render(
            "transactional/unsubscribe-newsletter.txt",
            user=user,
        ),
        render(
            "transactional/unsubscribe-newsletter.html",
            user=user,
        ),
    )

    return "250 Unsubscribe request accepted"


def handle_sender_email(envelope: Envelope):
    filename = (
        arrow.now().format("YYYY-MM-DD_HH-mm-ss") + "_" + random_string(10) + ".eml"
    )
    filepath = os.path.join(SENDER_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(envelope.original_content)

    LOG.d("Write email to sender at %s", filepath)

    msg = email.message_from_bytes(envelope.original_content)
    orig = get_orig_message_from_bounce(msg)
    if orig:
        LOG.warning(
            "Original message %s -> %s saved at %s", orig["From"], orig["To"], filepath
        )

    return "250 email to sender accepted"


def handle(envelope: Envelope) -> str:
    """Return SMTP status"""

    # sanitize mail_from, rcpt_tos
    mail_from = sanitize_email(envelope.mail_from)
    rcpt_tos = [sanitize_email(rcpt_to) for rcpt_to in envelope.rcpt_tos]
    envelope.mail_from = mail_from
    envelope.rcpt_tos = rcpt_tos

    # unsubscribe request
    if UNSUBSCRIBER and rcpt_tos == [UNSUBSCRIBER]:
        LOG.d("Handle unsubscribe request from %s", mail_from)
        return handle_unsubscribe(envelope)

    # emails sent to sender. Probably bounce emails
    if SENDER and rcpt_tos == [SENDER]:
        LOG.d("Handle email sent to sender from %s", mail_from)
        return handle_sender_email(envelope)

    if (
        len(rcpt_tos) == 1
        and rcpt_tos[0].startswith(BOUNCE_PREFIX)
        and rcpt_tos[0].endswith(BOUNCE_SUFFIX)
    ):
        return handle_bounce(envelope, rcpt_tos[0])

    # Whether it's necessary to apply greylisting
    if greylisting_needed(mail_from, rcpt_tos):
        LOG.warning("Grey listing applied for %s %s", mail_from, rcpt_tos)
        return "421 SL Retry later"

    # result of all deliveries
    # each element is a couple of whether the delivery is successful and the smtp status
    res: [(bool, str)] = []

    for rcpt_to in rcpt_tos:
        if rcpt_to == NOREPLY:
            LOG.exception("email sent to noreply address from %s", mail_from)
            return "550 SL E25 Email sent to noreply address"

        msg = email.message_from_bytes(envelope.original_content)

        # Reply case
        # recipient starts with "reply+" or "ra+" (ra=reverse-alias) prefix
        if is_reply_email(rcpt_to):
            LOG.debug("Reply phase %s(%s) -> %s", mail_from, msg["From"], rcpt_to)
            is_delivered, smtp_status = handle_reply(envelope, msg, rcpt_to)
            res.append((is_delivered, smtp_status))
        else:  # Forward case
            LOG.debug(
                "Forward phase %s(%s) -> %s",
                mail_from,
                msg["From"],
                rcpt_to,
            )
            for is_delivered, smtp_status in handle_forward(envelope, msg, rcpt_to):
                res.append((is_delivered, smtp_status))

    for (is_success, smtp_status) in res:
        # Consider all deliveries successful if 1 delivery is successful
        if is_success:
            return smtp_status

    # Failed delivery for all, return the first failure
    return res[0][1]


def handle_bounce(envelope, rcpt_to) -> str:
    """
    Return SMTP status, e.g. "500 Error"
    """
    LOG.d("handle bounce sent to %s", rcpt_to)
    msg = email.message_from_bytes(envelope.original_content)

    # parse the EmailLog
    email_log_id = parse_email_log_id_from_bounce(rcpt_to)
    email_log = EmailLog.get(email_log_id)

    if email_log.is_reply:
        handle_bounce_reply_phase(msg, email_log)
        return "550 SL E24 Email cannot be sent to contact"
    else:  # forward phase
        handle_bounce_forward_phase(msg, email_log)
        return "550 SL E26 Email cannot be forwarded to mailbox"


async def get_spam_score_async(message: Message) -> float:
    sa_input = to_bytes(message)

    # Spamassassin requires to have an ending linebreak
    if not sa_input.endswith(b"\n"):
        LOG.d("add linebreak to spamassassin input")
        sa_input += b"\n"

    try:
        # wait for at max 300s which is the default spamd timeout-child
        response = await asyncio.wait_for(
            aiospamc.check(sa_input, host=SPAMASSASSIN_HOST), timeout=300
        )
        return response.headers["Spam"].score
    except asyncio.TimeoutError:
        LOG.exception("SpamAssassin timeout")
        # return a negative score so the message is always considered as ham
        return -999
    except Exception:
        LOG.exception("SpamAssassin exception")
        return -999


def get_spam_score(
    message: Message, email_log: EmailLog, can_retry=True
) -> (float, dict):
    """
    Return the spam score and spam report
    """
    LOG.debug("get spam score for %s", email_log)
    sa_input = to_bytes(message)

    # Spamassassin requires to have an ending linebreak
    if not sa_input.endswith(b"\n"):
        LOG.d("add linebreak to spamassassin input")
        sa_input += b"\n"

    try:
        # wait for at max 300s which is the default spamd timeout-child
        sa = SpamAssassin(sa_input, host=SPAMASSASSIN_HOST, timeout=300)
        return sa.get_score(), sa.get_report_json()
    except Exception:
        if can_retry:
            LOG.warning("SpamAssassin exception, retry")
            time.sleep(3)
            return get_spam_score(message, email_log, can_retry=False)
        else:
            # return a negative score so the message is always considered as ham
            LOG.exception("SpamAssassin exception, ignore spam check")
            return -999, None


def sl_sendmail(
    from_addr, to_addr, msg: Message, mail_options, rcpt_options, can_retry=True
):
    """replace smtp.sendmail"""
    try:
        if POSTFIX_SUBMISSION_TLS:
            smtp = SMTP(POSTFIX_SERVER, 587)
            smtp.starttls()
        else:
            smtp = SMTP(POSTFIX_SERVER, POSTFIX_PORT or 25)

        # smtp.send_message has UnicodeEncodeError
        # encode message raw directly instead
        smtp.sendmail(
            from_addr,
            to_addr,
            to_bytes(msg),
            mail_options,
            rcpt_options,
        )
    except SMTPServerDisconnected:
        if can_retry:
            LOG.warning("SMTPServerDisconnected error, retry")
            time.sleep(3)
            sl_sendmail(
                from_addr, to_addr, msg, mail_options, rcpt_options, can_retry=False
            )
        else:
            raise


class MailHandler:
    async def handle_DATA(self, server, session, envelope: Envelope):
        try:
            ret = self._handle(envelope)
            return ret
        except Exception:
            LOG.exception(
                "email handling fail %s -> %s",
                envelope.mail_from,
                envelope.rcpt_tos,
            )
            return "421 SL Retry later"

    def _handle(self, envelope: Envelope):
        start = time.time()
        LOG.info(
            "===>> New message, mail from %s, rctp tos %s ",
            envelope.mail_from,
            envelope.rcpt_tos,
        )

        app = new_app()
        with app.app_context():
            ret = handle(envelope)
            LOG.info("takes %s seconds <<===", time.time() - start)
            return ret


def main(port: int):
    """Use aiosmtpd Controller"""
    controller = Controller(MailHandler(), hostname="0.0.0.0", port=port)

    controller.start()
    LOG.d("Start mail controller %s %s", controller.hostname, controller.port)

    if LOAD_PGP_EMAIL_HANDLER:
        LOG.warning("LOAD PGP keys")
        app = create_app()
        with app.app_context():
            load_pgp_public_keys()

    while True:
        time.sleep(2)


def asyncio_main(port: int):
    """
    Main entrypoint using asyncio directly without passing by aiosmtpd Controller
    """
    if LOAD_PGP_EMAIL_HANDLER:
        LOG.warning("LOAD PGP keys")
        app = create_app()
        with app.app_context():
            load_pgp_public_keys()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    lock = asyncio.Lock()
    handler = MailHandler(lock)

    def factory():
        return aiosmtpd.smtp.SMTP(handler, enable_SMTPUTF8=True)

    server = loop.run_until_complete(
        loop.create_server(factory, host="0.0.0.0", port=port)
    )

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    # Close the server
    LOG.info("Close SMTP server")
    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-p", "--port", help="SMTP port to listen for", type=int, default=20381
    )
    args = parser.parse_args()

    LOG.info("Listen for port %s", args.port)
    main(port=args.port)
