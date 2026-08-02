resource "google_billing_budget" "recovery" {
  count           = local.recovery_enabled ? 1 : 0
  billing_account = var.recovery_billing_account_id
  display_name    = "Blueprint recovery project monthly budget"

  budget_filter {
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
    projects               = ["projects/${data.google_project.recovery[0].number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.recovery_monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.25
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  depends_on = [google_project_service.required]
}
