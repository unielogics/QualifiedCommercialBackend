# Signed SES delivery feedback for the central outbox. The public webhook
# verifies the SNS certificate and confirms this HTTPS subscription itself.

resource "aws_sns_topic" "ses_delivery_events" {
  name = "qualified-commercial-ses-delivery-events"
}

data "aws_iam_policy_document" "ses_delivery_events" {
  statement {
    sid     = "AllowSesPublish"
    effect  = "Allow"
    actions = ["SNS:Publish"]

    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }

    resources = [aws_sns_topic.ses_delivery_events.arn]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "ses_delivery_events" {
  arn    = aws_sns_topic.ses_delivery_events.arn
  policy = data.aws_iam_policy_document.ses_delivery_events.json
}

resource "aws_sns_topic_subscription" "ses_delivery_events_webhook" {
  count = var.ses_feedback_subscription_enabled ? 1 : 0

  topic_arn = aws_sns_topic.ses_delivery_events.arn
  protocol  = "https"
  endpoint  = "https://${var.api_subdomain}.${var.domain_root}/api/v1/webhooks/ses"
}

resource "aws_ses_event_destination" "central_outbox" {
  count                  = var.ses_feedback_subscription_enabled && var.ses_configuration_set != "" ? 1 : 0
  name                   = "qualified-commercial-central-outbox"
  configuration_set_name = var.ses_configuration_set
  enabled                = true
  matching_types = [
    "send",
    "reject",
    "bounce",
    "complaint",
    "delivery",
    "open",
    "click",
    "renderingFailure",
  ]

  sns_destination {
    topic_arn = aws_sns_topic.ses_delivery_events.arn
  }

  depends_on = [aws_sns_topic_policy.ses_delivery_events]
}
