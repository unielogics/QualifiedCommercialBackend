# Secrets Manager secret + the IAM role the EC2 uses to read it.

resource "aws_secretsmanager_secret" "qcbackend" {
  name        = "qcbackend/prod"
  description = "Runtime env for the qcbackend container."

  # 7-day recovery window — enough for accidental delete recovery, fast enough
  # to re-create with the same name if you need to rotate.
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "qcbackend" {
  secret_id     = aws_secretsmanager_secret.qcbackend.id
  secret_string = jsonencode(var.secret_payload)
}

# ---------- Instance role ----------

resource "aws_iam_role" "qcbackend_instance" {
  name = "qcbackend-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Read the prod secret only — least privilege.
resource "aws_iam_role_policy" "qcbackend_read_secret" {
  name = "qcbackend-read-secret"
  role = aws_iam_role.qcbackend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ]
      Resource = aws_secretsmanager_secret.qcbackend.arn
    }]
  })
}

# Allow PUT/GET on the documents bucket (created on first write — see app/services/storage).
resource "aws_iam_role_policy" "qcbackend_s3" {
  name = "qcbackend-s3"
  role = aws_iam_role.qcbackend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:PutObjectAcl",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ]
      Resource = [
        "arn:aws:s3:::qc-documents-*",
        "arn:aws:s3:::qc-documents-*/*"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "qcbackend_bedrock" {
  name = "qcbackend-bedrock"
  role = aws_iam_role.qcbackend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles",
          "bedrock:ListFoundationModels"
        ]
        Resource = "*"
      }
    ]
  })
}

# SSM Session Manager + patch agent — lets us shell into the box without SSH keys.
resource "aws_iam_role_policy_attachment" "qcbackend_ssm" {
  role       = aws_iam_role.qcbackend_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "qcbackend" {
  name = "qcbackend-instance-profile"
  role = aws_iam_role.qcbackend_instance.name
}
