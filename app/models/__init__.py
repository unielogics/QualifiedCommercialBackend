"""ORM models — single import surface so Alembic autogenerate sees everything."""

from app.models.activity import Activity  # noqa: F401
from app.models.agent_reassignment_audit import AgentReassignmentAudit  # noqa: F401
from app.models.ai_agent import (  # noqa: F401
    AIAgent,
    AIAgentExitRules,
    AIAgentGoal,
    AIAgentKnowledgeLink,
    AIAgentLead,
    AIAgentMessage,
    AIAgentPlaybook,
    AIAgentShowingGuide,
    AIAgentTargeting,
    AIAgentTestScenario,
    AIAgentTrainingMessage,
    AIAgentTrainingSession,
    AIVoiceProfile,
)
from app.models.ai_audit_event import AIAuditEvent  # noqa: F401
from app.models.ai_cadence_rule import AICadenceRule  # noqa: F401
from app.models.ai_chat_thread import AIChatMessage, AIChatThread  # noqa: F401
from app.models.ai_feedback import AIFeedback  # noqa: F401
from app.models.ai_knowledge_document import AIKnowledgeDocument  # noqa: F401
from app.models.ai_modify_correction import AIModifyCorrection  # noqa: F401
from app.models.ai_outreach_event import AIOutreachEvent  # noqa: F401
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate  # noqa: F401
from app.models.ai_task import AITask  # noqa: F401
from app.models.ai_token_usage import AITokenUsage  # noqa: F401
from app.models.ai_task_assignment import AITaskAssignment  # noqa: F401
from app.models.ai_usage_event import AIUsageEvent  # noqa: F401
from app.models.app_settings import AppSettings  # noqa: F401
from app.models.admin_activity import AdminActivitySeen, AdminDigestState  # noqa: F401
from app.models.analysis_run import AnalysisRun  # noqa: F401
from app.models.booking_settings import BookingSettings  # noqa: F401
from app.models.broker import Broker  # noqa: F401
from app.models.contract_agreement import ContractAgreement  # noqa: F401
from app.models.deal_registration import DealRegistration  # noqa: F401
from app.models.dealer_lead_channel_seen import DealerLeadChannelSeen  # noqa: F401
from app.models.referral_partner_company import ReferralPartnerCompany  # noqa: F401
from app.models.billing import (  # noqa: F401
    BillableExpense,
    ChargeAttempt,
    ClientPaymentMethod,
    ESignEvent,
    PaymentAuthorization,
)
from app.models.bucket import (  # noqa: F401
    Bucket,
    BucketActivityLog,
    BucketAIActionItem,
    BucketAIMessage,
    BucketAIReview,
    BucketDocumentSignature,
    BucketDocumentTemplate,
    BucketFile,
    BucketFileAnalysis,
    BucketFileAnnotation,
    BucketNote,
    BucketRequestedDocument,
    BucketShare,
    BucketUploadLink,
    BucketVendorAccess,
)
from app.models.capital_partner_application import (  # noqa: F401
    APPLICATION_STATUSES,
    CapitalPartnerApplication,
)
from app.models.client import Client  # noqa: F401
from app.models.client_ai_plan import ClientAIPlan  # noqa: F401
from app.models.client_property import ClientProperty  # noqa: F401
from app.models.client_requirement_status import ClientRequirementStatus  # noqa: F401
from app.models.closing_cost_tier import ClosingCostTier  # noqa: F401
from app.models.credit_pull import CreditPull  # noqa: F401
from app.models.deal import Deal  # noqa: F401
from app.models.deal_chat_message import DealChatMessage  # noqa: F401
from app.models.dealer_intake_login import DealerIntakeLoginChallenge  # noqa: F401
from app.models.agent_task import AgentTask  # noqa: F401
from app.models.device_token import DeviceToken  # noqa: F401
from app.models.document import Document  # noqa: F401
from app.models.document_analysis_result import DocumentAnalysisResult  # noqa: F401
from app.models.email_draft import EmailDraft  # noqa: F401
from app.models.email_message import EmailMessage  # noqa: F401
from app.models.event import CalendarEvent  # noqa: F401
from app.models.fred_observation import FredObservation  # noqa: F401
from app.models.google_account import GoogleAccount  # noqa: F401
from app.models.hud import HudLineItem  # noqa: F401
from app.models.hud_share_link import HudShareLink  # noqa: F401
from app.models.legal_acceptance import LegalAcceptance  # noqa: F401
from app.models.lender import Lender  # noqa: F401
from app.models.lender_package import (  # noqa: F401
    LenderPackage,
    LenderPackageDocument,
    LenderPackageEvent,
    LenderPackageRecipient,
    LenderTerm,
    LenderUser,
)
from app.models.lender_spread import LenderSpread  # noqa: F401
from app.models.lending_handoff_packet import LendingHandoffPacket  # noqa: F401
from app.models.loan import Loan  # noqa: F401
from app.models.loan_chat_message import LoanChatMessage  # noqa: F401
from app.models.loan_instruction import LoanInstruction  # noqa: F401
from app.models.loan_participant import LoanParticipant  # noqa: F401
from app.models.loan_scenario import LoanScenario  # noqa: F401
from app.models.fix_flip_scenario import FixFlipScenario  # noqa: F401
from app.models.message import Message  # noqa: F401
from app.models.message_attachment import MessageAttachment  # noqa: F401
from app.models.notification import Notification  # noqa: F401
from app.models.operator_file import BucketIntakeLink, BucketIntakeLinkFile  # noqa: F401
from app.models.prequal_request import PrequalRequest  # noqa: F401
from app.models.property_intelligence import PropertyIntelligenceSnapshot  # noqa: F401
from app.models.provider_secret import ProviderSecret  # noqa: F401
from app.models.provider_usage_event import ProviderUsageEvent  # noqa: F401
from app.models.public_underwriting_intake import (  # noqa: F401
    PublicUnderwritingIntake,
    PublicUnderwritingIntakeArtifact,
    PublicUnderwritingIntakeEmailSend,
)
from app.models.rate_sheet import RateSheetEntry  # noqa: F401
from app.models.regional_manager import RegionalManagerAgent  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.vector_log import VectorLog  # noqa: F401
from app.dealer_os import models as dealer_os_models  # noqa: F401  (Dealer OS — isolated dos_* tables)
