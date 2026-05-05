"""ORM models — single import surface so Alembic autogenerate sees everything."""

from app.models.activity import Activity  # noqa: F401
from app.models.ai_feedback import AIFeedback  # noqa: F401
from app.models.ai_modify_correction import AIModifyCorrection  # noqa: F401
from app.models.ai_task import AITask  # noqa: F401
from app.models.app_settings import AppSettings  # noqa: F401
from app.models.broker import Broker  # noqa: F401
from app.models.client import Client  # noqa: F401
from app.models.credit_pull import CreditPull  # noqa: F401
from app.models.document import Document  # noqa: F401
from app.models.email_draft import EmailDraft  # noqa: F401
from app.models.event import CalendarEvent  # noqa: F401
from app.models.fred_observation import FredObservation  # noqa: F401
from app.models.hud import HudLineItem  # noqa: F401
from app.models.lender import Lender  # noqa: F401
from app.models.lender_spread import LenderSpread  # noqa: F401
from app.models.loan import Loan  # noqa: F401
from app.models.loan_chat_message import LoanChatMessage  # noqa: F401
from app.models.loan_instruction import LoanInstruction  # noqa: F401
from app.models.loan_participant import LoanParticipant  # noqa: F401
from app.models.loan_scenario import LoanScenario  # noqa: F401
from app.models.message import Message  # noqa: F401
from app.models.rate_sheet import RateSheetEntry  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.vector_log import VectorLog  # noqa: F401
