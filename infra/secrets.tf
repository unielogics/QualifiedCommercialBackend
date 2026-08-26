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
    Statement = [
      {
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
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
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
      },
      {
        Effect = "Allow"
        Action = [
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Subscribe",
          "bedrock:PutUseCaseForModelAccess",
          "bedrock:ListFoundationModelAgreementOffers",
          "bedrock:CreateFoundationModelAgreement",
          "bedrock:GetFoundationModelAvailability",
          "bedrock:GetFoundationModel"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "qcbackend_ses_send" {
  name = "qcbackend-ses-qualifiedcommercial-send"
  role = aws_iam_role.qcbackend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ses:SendEmail",
        "ses:SendRawEmail"
      ]
      Resource = concat(
        [
          "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${var.domain_root}"
        ],
        var.ses_configuration_set != "" ? [
          "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:configuration-set/${var.ses_configuration_set}"
        ] : []
      )
      Condition = {
        StringEquals = {
          "ses:FromAddress" = var.ses_from_address
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "qcbackend_sms" {
  name = "qcbackend-sms-end-user-messaging"
  role = aws_iam_role.qcbackend_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SendTransactionalSmsFromConfiguredIdentities"
        Effect = "Allow"
        Action = [
          "sms-voice:SendTextMessage"
        ]
        Resource = [
          "arn:aws:sms-voice:${var.aws_region}:${data.aws_caller_identity.current.account_id}:phone-number/*",
          "arn:aws:sms-voice:${var.aws_region}:${data.aws_caller_identity.current.account_id}:pool/*"
        ]
      },
      {
        Sid    = "ReadConfiguredSmsIdentities"
        Effect = "Allow"
        Action = [
          "sms-voice:DescribePhoneNumbers"
        ]
        Resource = "arn:aws:sms-voice:${var.aws_region}:${data.aws_caller_identity.current.account_id}:phone-number/*"
      },
      {
        Sid    = "ReadConfiguredSmsPools"
        Effect = "Allow"
        Action = [
          "sms-voice:DescribePools",
          "sms-voice:ListPoolOriginationIdentities"
        ]
        Resource = "arn:aws:sms-voice:${var.aws_region}:${data.aws_caller_identity.current.account_id}:pool/*"
      },
      {
        Sid    = "ReadSmsAccountAttributes"
        Effect = "Allow"
        Action = [
          "sms-voice:DescribeAccountAttributes"
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
