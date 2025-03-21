import arrow
import sqlalchemy

from app import models
from flask import redirect, url_for, request, flash
from flask_admin import expose, AdminIndexView
from flask_admin.actions import action
from flask_admin.contrib import sqla
from flask_login import current_user

from app.db import Session
from app.models import (
    User,
    ManualSubscription,
    Fido,
    Subscription,
    AppleSubscription,
    AdminAuditLog,
    AuditLogActionEnum,
)


class SLModelView(sqla.ModelView):
    column_default_sort = ("id", True)
    column_display_pk = True

    can_edit = False
    can_create = False
    can_delete = False
    edit_modal = True

    def is_accessible(self):
        return current_user.is_authenticated and current_user.is_admin

    def inaccessible_callback(self, name, **kwargs):
        # redirect to login page if user doesn't have access
        return redirect(url_for("auth.login", next=request.url))

    def on_model_change(self, form, model, is_created):
        changes = {}
        for attr in sqlalchemy.inspect(model).attrs:
            if attr.history.has_changes() and attr.key not in (
                "created_at",
                "updated_at",
            ):
                value = attr.value
                # If it's a model reference, get the source id
                if issubclass(type(value), models.Base):
                    value = value.id
                # otherwise, if its a generic object stringify it
                if issubclass(type(value), object):
                    value = str(value)
                changes[attr.key] = value
        auditAction = (
            AuditLogActionEnum.create_object
            if is_created
            else AuditLogActionEnum.update_object
        )
        AdminAuditLog.create(
            admin_user_id=current_user.id,
            model=model.__class__.__name__,
            model_id=model.id,
            action=auditAction.value,
            data=changes,
        )

    def on_model_delete(self, model):
        AdminAuditLog.create(
            admin_user_id=current_user.id,
            model=model.__class__.__name__,
            model_id=model.id,
            action=AuditLogActionEnum.delete_object.value,
        )


class SLAdminIndexView(AdminIndexView):
    @expose("/")
    def index(self):
        if not current_user.is_authenticated or not current_user.is_admin:
            return redirect(url_for("auth.login", next=request.url))

        return redirect("/admin/user")


class UserAdmin(SLModelView):
    column_searchable_list = ["email", "id"]
    column_exclude_list = [
        "salt",
        "password",
        "otp_secret",
        "last_otp",
        "fido_uuid",
        "profile_picture",
    ]
    can_edit = False

    def scaffold_list_columns(self):
        ret = super().scaffold_list_columns()
        ret.insert(0, "upgrade_channel")
        return ret

    @action(
        "education_upgrade",
        "Education upgrade",
        "Are you sure you want to edu-upgrade selected users?",
    )
    def action_edu_upgrade(self, ids):
        manual_upgrade("Edu", ids, is_giveaway=True)

    @action(
        "charity_org_upgrade",
        "Charity Organization upgrade",
        "Are you sure you want to upgrade selected users using the Charity organization program?",
    )
    def action_charity_org_upgrade(self, ids):
        manual_upgrade("Charity Organization", ids, is_giveaway=True)

    @action(
        "journalist_upgrade",
        "Journalist upgrade",
        "Are you sure you want to upgrade selected users using the Journalist program?",
    )
    def action_journalist_upgrade(self, ids):
        manual_upgrade("Journalist", ids, is_giveaway=True)

    @action(
        "cash_upgrade",
        "Cash upgrade",
        "Are you sure you want to cash-upgrade selected users?",
    )
    def action_cash_upgrade(self, ids):
        manual_upgrade("Cash", ids, is_giveaway=False)

    @action(
        "crypto_upgrade",
        "Crypto upgrade",
        "Are you sure you want to crypto-upgrade selected users?",
    )
    def action_monero_upgrade(self, ids):
        manual_upgrade("Crypto", ids, is_giveaway=False)

    @action(
        "adhoc_upgrade",
        "Adhoc upgrade - for exceptional case",
        "Are you sure you want to crypto-upgrade selected users?",
    )
    def action_adhoc_upgrade(self, ids):
        manual_upgrade("Adhoc", ids, is_giveaway=False)

    @action(
        "extend_trial_1w",
        "Extend trial for 1 week more",
        "Extend trial for 1 week more?",
    )
    def extend_trial_1w(self, ids):
        for user in User.filter(User.id.in_(ids)):
            if user.trial_end and user.trial_end > arrow.now():
                user.trial_end = user.trial_end.shift(weeks=1)
            else:
                user.trial_end = arrow.now().shift(weeks=1)

            flash(f"Extend trial for {user} to {user.trial_end}", "success")
            AdminAuditLog.extend_trial(
                current_user.id, user.id, user.trial_end, "1 week"
            )

        Session.commit()

    @action(
        "disable_otp_fido",
        "Disable OTP & FIDO",
        "Disable OTP & FIDO?",
    )
    def disable_otp_fido(self, ids):
        for user in User.filter(User.id.in_(ids)):
            user_had_otp = user.enable_otp
            if user.enable_otp:
                user.enable_otp = False
                flash(f"Disable OTP for {user}", "info")

            user_had_fido = user.fido_uuid is not None
            if user.fido_uuid:
                Fido.filter_by(uuid=user.fido_uuid).delete()
                user.fido_uuid = None
                flash(f"Disable FIDO for {user}", "info")
            AdminAuditLog.disable_otp_fido(
                current_user.id, user.id, user_had_otp, user_had_fido
            )

        Session.commit()

    # @action(
    #     "login_as",
    #     "Login as this user",
    #     "Login as this user?",
    # )
    # def login_as(self, ids):
    #     if len(ids) != 1:
    #         flash("only 1 user can be selected", "error")
    #         return
    #
    #     for user in User.filter(User.id.in_(ids)):
    #         AdminAuditLog.logged_as_user(current_user.id, user.id)
    #         login_user(user)
    #         flash(f"Login as user {user}", "success")
    #         return redirect("/")


def manual_upgrade(way: str, ids: [int], is_giveaway: bool):
    for user in User.filter(User.id.in_(ids)).all():
        if user.lifetime:
            flash(f"user {user} already has a lifetime license", "warning")
            continue

        sub: Subscription = user.get_subscription()
        if sub and not sub.cancelled:
            flash(
                f"user {user} already has a Paddle license, they have to cancel it first",
                "warning",
            )
            continue

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=user.id)
        if apple_sub and apple_sub.is_valid():
            flash(
                f"user {user} already has a Apple subscription, they have to cancel it first",
                "warning",
            )
            continue

        AdminAuditLog.create_manual_upgrade(current_user.id, way, user.id, is_giveaway)
        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=user.id)
        if manual_sub:
            # renew existing subscription
            if manual_sub.end_at > arrow.now():
                manual_sub.end_at = manual_sub.end_at.shift(years=1)
            else:
                manual_sub.end_at = arrow.now().shift(years=1, days=1)
            flash(f"Subscription extended to {manual_sub.end_at.humanize()}", "success")
            continue

        ManualSubscription.create(
            user_id=user.id,
            end_at=arrow.now().shift(years=1, days=1),
            comment=way,
            is_giveaway=is_giveaway,
        )

        flash(f"New {way} manual subscription for {user} is created", "success")
    Session.commit()


class EmailLogAdmin(SLModelView):
    column_searchable_list = ["id"]
    column_filters = ["id", "user.email", "mailbox.email", "contact.website_email"]

    can_edit = False
    can_create = False


class AliasAdmin(SLModelView):
    column_searchable_list = ["id", "user.email", "email", "mailbox.email"]
    column_filters = ["id", "user.email", "email", "mailbox.email"]


class MailboxAdmin(SLModelView):
    column_searchable_list = ["id", "user.email", "email"]
    column_filters = ["id", "user.email", "email"]


# class LifetimeCouponAdmin(SLModelView):
#     can_edit = True
#     can_create = True


class CouponAdmin(SLModelView):
    can_edit = False
    can_create = True


class ManualSubscriptionAdmin(SLModelView):
    can_edit = True
    column_searchable_list = ["id", "user.email"]

    @action(
        "extend_1y",
        "Extend for 1 year",
        "Extend 1 year more?",
    )
    def extend_1y(self, ids):
        for ms in ManualSubscription.filter(ManualSubscription.id.in_(ids)):
            ms.end_at = ms.end_at.shift(years=1)
            flash(f"Extend subscription for 1 year for {ms.user}", "success")
            AdminAuditLog.extend_subscription(
                current_user.id, ms.user.id, ms.end_at, "1 year"
            )

        Session.commit()

    @action(
        "extend_1m",
        "Extend for 1 month",
        "Extend 1 month more?",
    )
    def extend_1m(self, ids):
        for ms in ManualSubscription.filter(ManualSubscription.id.in_(ids)):
            ms.end_at = ms.end_at.shift(months=1)
            flash(f"Extend subscription for 1 month for {ms.user}", "success")
            AdminAuditLog.extend_subscription(
                current_user.id, ms.user.id, ms.end_at, "1 month"
            )

        Session.commit()


# class ClientAdmin(SLModelView):
#     column_searchable_list = ["name", "description", "user.email"]
#     column_exclude_list = ["oauth_client_secret", "home_url"]
#     can_edit = True


class CustomDomainAdmin(SLModelView):
    column_searchable_list = ["domain", "user.email", "user.id"]
    column_exclude_list = ["ownership_txt_token"]
    can_edit = False


class ReferralAdmin(SLModelView):
    column_searchable_list = ["id", "user.email", "code", "name"]
    column_filters = ["id", "user.email", "code", "name"]

    def scaffold_list_columns(self):
        ret = super().scaffold_list_columns()
        ret.insert(0, "nb_user")
        ret.insert(0, "nb_paid_user")
        return ret


# class PayoutAdmin(SLModelView):
#     column_searchable_list = ["id", "user.email"]
#     column_filters = ["id", "user.email"]
#     can_edit = True
#     can_create = True
#     can_delete = True


def _admin_action_formatter(view, context, model, name):
    action_name = AuditLogActionEnum.get_name(model.action)
    return "{} ({})".format(action_name, model.action)


def _admin_created_at_formatter(view, context, model, name):
    return model.created_at.format()


class AdminAuditLogAdmin(SLModelView):
    column_searchable_list = ["admin.id", "admin.email", "model_id", "created_at"]
    column_filters = ["admin.id", "admin.email", "model_id", "created_at"]
    column_exclude_list = ["id"]
    column_hide_backrefs = False
    can_edit = False
    can_create = False
    can_delete = False

    column_formatters = {
        "action": _admin_action_formatter,
        "created_at": _admin_created_at_formatter,
    }
