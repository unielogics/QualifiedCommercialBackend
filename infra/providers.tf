provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "qualified-commercial"
      Service   = "qcbackend"
      ManagedBy = "terraform"
    }
  }
}
