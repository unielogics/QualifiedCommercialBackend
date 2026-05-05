terraform {
  backend "s3" {
    bucket         = "qc-terraform-state-156041400244"
    key            = "qcbackend/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "qc-terraform-locks"
    encrypt        = true
  }
}
