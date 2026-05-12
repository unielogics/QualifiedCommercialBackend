"""ORM models — single import surface so Alembic autogenerate sees everything."""

from app.models.activity import Activity  # noqa: F401
from app.models.ai_audit_event import AIAuditEvent  # noqa: F401
from app.models.ai_cadence_rule import AICadenceRule  # noqa: F401
from app.models.ai_chat_thread import AIChatMessage, AIChatThread  # noqa: F401
from app.models.ai_feedback import AIFeedback  # noqa: F401
from app.models.ai_knowledge_document import AIKnowledgeDocument  # noqa: F401
from app.models.ai_modify_correction import AIModifyCorrection  # noqa: F401
from app.models.ai_outreach_event import AIOutreachEvent  # noqa: F401
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate  # noqa: F401
from app.models.ai_task import AITask  # noqa: F401
from app.models.ai_task_assignment import AITaskAssignment  # noqa: F401
from app.models.app_settings import AppSettings  # noqa: F401
from app.models.broker import Broker  # noqa: F401
from app.models.client import Client  # noqa: F401
from app.models.client_ai_plan import ClientAIPlan  # noqa: F401
from app.models.client_property import ClientProperty  # noqa: F401
from app.models.client_requirement_status import ClientRequirementStatus  # noqa: F401
from app.models.credit_pull import CreditPull  # noqa: F401
from app.models.deal import Deal  # noqa: F401
from app.models.agent_task import AgentTask  # noqa: F401
from app.models.device_token import DeviceToken  # noqa: F401
from app.models.document import Document  # noqa: F401
from app.models.document_analysis_result import DocumentAnalysisResult  # noqa: F401
from app.models.email_draft import EmailDraft  # noqa: F401
from app.models.event import CalendarEvent  # noqa: F401
from app.models.fred_observation import FredObservation  # noqa: F401
from app.models.hud import HudLineItem  # noqa: F401
from app.models.hud_share_link import HudShareLink  # noqa: F401
from app.models.legal_acceptance import LegalAcceptance  # noqa: F401
from app.models.lender import Lender  # noqa: F401
from app.models.lender_spread import LenderSpread  # noqa: F401
from app.models.lending_handoff_packet import LendingHandoffPacket  # noqa: F401
from app.models.loan import Loan  # noqa: F401
from app.models.loan_chat_message import LoanChatMessage  # noqa: F401
from app.models.loan_instruction import LoanInstruction  # noqa: F401
from app.models.loan_participant import LoanParticipant  # noqa: F401
from app.models.loan_scenario import LoanScenario  # noqa: F401
from app.models.message import Message  # noqa: F401
from app.models.prequal_request import PrequalRequest  # noqa: F401
from app.models.rate_sheet import RateSheetEntry  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.vector_log import VectorLog  # noqa: F401
