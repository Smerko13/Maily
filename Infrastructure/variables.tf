variable "google_client_id" {
  description = "Google OAuth Client ID for the Google authentication Lambda function"
  type        = string
  sensitive = true
  nullable = false
}

variable "google_client_secret" {
  description = "Google OAuth Client Secret for the Google authentication Lambda function"
  type        = string
  sensitive = true
  nullable = false
}

variable "openai_api_key" {
  description = "OpenAI API Key for the OpenAI integration in the Lambda function"
  type        = string
  sensitive = true
  nullable = false
}