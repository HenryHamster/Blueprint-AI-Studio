# ------------------------------------------------------------------------------
# Budget alert (BACKSTOP — the app-level check is the primary signal).
#
# Emails the alert_email at 50%, 90%, 100% of monthly_budget_usd. This DOES NOT
# auto-stop spend — it only warns.
#
# The PRIMARY budget signal is now the application-level monthly budget check
# (backend/app/monthly_budget_alerts.py), which monitors Blueprint's own cost
# telemetry against the operator-configurable runtime_config knob
# ``monthly_budget_target_usd`` — editable live from the admin UI, no deploy
# needed.  This GCP-native resource is a deliberately-high backstop so there
# is still a hard platform-level circuit-breaker-adjacent signal even if the
# application-level check has a bug.
#
# Combined with var.monthly_budget_usd default of $5000, the GCP backstop fires
# at a 'something is actually broken' level — well above the real target set
# via runtime config.
#
# Gated on both billing_account_id (required by API) and alert_email
# (the only configured notification target). Setting either to "" disables.
#
# Bootstrap: requires `gcloud services enable cloudbilling.googleapis.com` and
# `gcloud services enable billingbudgets.googleapis.com`. setup.sh enables both.
#
# Find your billing account ID with: `gcloud billing accounts list`
#
# Notification IAM caveat: Cloud Billing's notification service account needs
# `roles/billing.costsManager` (or roles/monitoring.notificationChannelEditor)
# on the project to write to the Cloud Monitoring channel. If the budget
# resource creates successfully but you never get email, that's the cause —
# bind the Cloud Billing budget service account
# (service-<NUMBER>@gcp-sa-billingbudgets.iam.gserviceaccount.com) to the
# missing role. GCP usually handles this auto-binding on first budget create.
# ------------------------------------------------------------------------------

locals {
  budget_enabled = var.billing_account_id != "" && local.alerting_enabled
}

data "google_project" "budget_project" {
  project_id = var.project_id
}

resource "google_billing_budget" "monthly" {
  count           = local.budget_enabled ? 1 : 0
  billing_account = var.billing_account_id
  display_name    = "Blueprint monthly budget"

  budget_filter {
    # The Billing API canonicalizes project IDs to project numbers. Use the
    # canonical form up front so every post-apply plan stays drift-free.
    projects = ["projects/${data.google_project.budget_project.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      # GCP API expects units as int64-as-string. Provider 5.x typically auto-
      # converts a number, but tostring() removes the silent-coercion risk.
      units = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [
      google_monitoring_notification_channel.email[0].id,
    ]
    disable_default_iam_recipients = false
  }
}
