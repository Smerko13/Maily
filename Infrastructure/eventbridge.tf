resource "aws_cloudwatch_event_rule" "maily_sync_schedule" {
  name                = "maily-sync-schedule"
  description         = "Triggers the Maily backend Lambda every 15 minutes to sync all connected Gmail accounts"
  schedule_expression = "rate(15 minutes)"

  tags = {
    Project     = "Maily"
    Environment = "Development"
  }
}

resource "aws_cloudwatch_event_target" "maily_sync_target" {
  rule      = aws_cloudwatch_event_rule.maily_sync_schedule.name
  target_id = "MailySyncLambda"
  arn       = aws_lambda_function.maily_backend_lambda.arn
}

resource "aws_lambda_permission" "allow_eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.maily_backend_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.maily_sync_schedule.arn
}
