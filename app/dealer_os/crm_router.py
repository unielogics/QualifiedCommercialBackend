"""Field Desk CRM, catalog, product discovery, and presentation endpoints."""

from __future__ import annotations

import hashlib
import html
import io
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.booking_settings import BookingSettings
from app.models.user import User
from app.services.email import ses_client
from app.services.payment_authorization import primary_super_admin

from .crm_schemas import (
    CompanyContactIn,
    ContactAssignmentIn,
    FinderAnswersIn,
    FundingGoalConfirmIn,
    ProductCatalogUpdate,
    ProductPresentationIn,
)
from .deps import require_super_admin, require_team_or_rep
from .models import (
    DealerApplicationContact,
    DealerBusiness,
    DealerProductCatalog,
    DealerProductFinderSession,
    DealerProductPresentation,
    DealerProductPresentationArtifact,
    DealerProductScreeningSnapshot,
    DealerRepCompany,
    DealerRepContact,
    DealerRepContactAssignment,
    DealerRepInboxMessage,
    DealerRepInboxThread,
    DealerRepLead,
    DealerSourceConnection,
)
from .services import buckets_link, consent_delivery, storage
from .services.product_finder import QUESTIONS, screen_products
from .services.targets import propose_targets

router = APIRouter(prefix="/dealer-os", tags=["dealer-os-crm"])

_CATALOG_SUMMARIES = {
    "term_loan_3_5_year": {"en": "Structured working capital or debt refinance with predictable monthly payments.", "es": "Capital de trabajo o refinanciamiento de deuda con pagos mensuales predecibles."},
    "term_loan_10_year": {"en": "Long-term, unsecured working capital for qualified small businesses.", "es": "Capital de trabajo a largo plazo y sin garantía para pequeños negocios calificados."},
    "line_of_credit": {"en": "Reusable working-capital access for seasonal and operating needs.", "es": "Acceso reutilizable a capital de trabajo para necesidades estacionales y operativas."},
    "term_loan_loc_hybrid": {"en": "A structured term facility combined with flexible revolving access.", "es": "Un préstamo estructurado combinado con acceso rotativo flexible."},
    "equipment_financing": {"en": "Asset-backed financing for vehicles, machinery, and business equipment.", "es": "Financiamiento respaldado por activos para vehículos, maquinaria y equipo comercial."},
    "jumbo_term_loan": {"en": "Larger structured capital for established businesses and complex transactions.", "es": "Capital estructurado de mayor tamaño para negocios establecidos y transacciones complejas."},
    "transportation_finance": {"en": "Equipment and operating capital tailored to transportation businesses.", "es": "Equipo y capital operativo adaptado a empresas de transporte."},
    "sba": {"en": "Government-backed financing for acquisitions, expansion, real estate, and working capital.", "es": "Financiamiento respaldado por el gobierno para adquisiciones, expansión, bienes raíces y capital de trabajo."},
    "sba_grocery": {"en": "SBA-oriented financing for grocery and food-market operators.", "es": "Financiamiento SBA para operadores de supermercados y mercados de alimentos."},
    "sba_made_in_america": {"en": "SBA-oriented capital supporting eligible domestic manufacturing investment.", "es": "Capital SBA para apoyar inversiones elegibles de manufactura nacional."},
}

_DIRECT_APPLICATION_KEYS = {"term_loan_3_5_year", "term_loan_10_year"}

_CATALOG_DETAILS: dict[str, dict[str, dict[str, object]]] = {
    "term_loan_3_5_year": {
        "en": {
            "closing_timeline": "Structured review; timing depends on a complete submission.",
            "uses": ["Working capital", "Debt refinancing"],
            "best_fit": ["Established businesses needing predictable monthly payments", "Requests from $25,000 to $500,000"],
            "minimum_requirements": ["2+ years in business", "660+ self-reported credit threshold", "$50,000+ annual revenue", "U.S. citizen or legal permanent resident", "Maximum one MCA", "Three positive month-end balances"],
            "documents": ["Official business bank statements", "Business tax returns", "Current debt and MCA statements", "Owner identification and authorization"],
            "exclusions": ["Bankruptcy within 7 years", "Felony within 10 years", "Active legal charges or OFAC match", "Trucking/warehousing, auto or boat dealers, medical labs, and other restricted industries"],
        },
        "es": {
            "closing_timeline": "Revision estructurada; el plazo depende de un expediente completo.",
            "uses": ["Capital de trabajo", "Refinanciamiento de deuda"],
            "best_fit": ["Negocios establecidos que buscan pagos mensuales predecibles", "Solicitudes de $25,000 a $500,000"],
            "minimum_requirements": ["2+ anos en operacion", "Umbral de credito declarado de 660+", "Ingresos anuales de $50,000+", "Ciudadano de EE. UU. o residente permanente", "Maximo un MCA", "Tres saldos positivos al cierre del mes"],
            "documents": ["Estados bancarios comerciales oficiales", "Declaraciones de impuestos del negocio", "Estados de deuda y MCA actuales", "Identificacion y autorizacion de propietarios"],
            "exclusions": ["Bancarrota dentro de 7 anos", "Delito grave dentro de 10 anos", "Cargos legales activos o coincidencia OFAC", "Transporte/almacenamiento, concesionarios de autos o barcos, laboratorios y otras industrias restringidas"],
        },
    },
    "term_loan_10_year": {
        "en": {
            "closing_timeline": "Structured review; timing depends on a complete submission.",
            "uses": ["Working capital only"],
            "best_fit": ["Qualified small businesses seeking long-term unsecured capital", "Requests from $15,000 to $50,000"],
            "minimum_requirements": ["2+ years in business", "660+ self-reported credit threshold", "Business DSCR of 1.10x+", "Maximum five owners", "No more than two NSF charges and five negative-balance days", "Up to two MCA/SBA balances funded more than 90 days ago"],
            "documents": ["Official business bank statements", "Business financials supporting DSCR", "Current debt statements", "Owner identification and authorization"],
            "exclusions": ["Debt refinancing", "Bankruptcy or foreclosure within 3 years", "Any felony", "Misdemeanor within 5 years", "Open tax liens or judgments", "Trucking/logistics, auto/RV/boat dealers, restaurants, and SBA-ineligible industries"],
        },
        "es": {
            "closing_timeline": "Revision estructurada; el plazo depende de un expediente completo.",
            "uses": ["Solo capital de trabajo"],
            "best_fit": ["Pequenos negocios calificados que buscan capital sin garantia a largo plazo", "Solicitudes de $15,000 a $50,000"],
            "minimum_requirements": ["2+ anos en operacion", "Umbral de credito declarado de 660+", "DSCR comercial de 1.10x+", "Maximo cinco propietarios", "No mas de dos NSF y cinco dias con saldo negativo", "Hasta dos saldos MCA/SBA financiados hace mas de 90 dias"],
            "documents": ["Estados bancarios comerciales oficiales", "Financieros que respalden el DSCR", "Estados de deuda actuales", "Identificacion y autorizacion de propietarios"],
            "exclusions": ["Refinanciamiento de deuda", "Bancarrota o ejecucion hipotecaria dentro de 3 anos", "Cualquier delito grave", "Delito menor dentro de 5 anos", "Gravamen fiscal o sentencia abierta", "Transporte, concesionarios, restaurantes e industrias no elegibles por SBA"],
        },
    },
}

_CATALOG_DETAILS.update({
    "line_of_credit": {
        "en": {
            "closing_timeline": "Timing depends on bank review and a complete operating profile.",
            "uses": ["Recurring working capital", "Seasonal inventory", "Short-term operating gaps"],
            "best_fit": ["Established businesses with repeat borrowing needs", "Companies seeking reusable access rather than one lump-sum loan"],
            "minimum_requirements": ["Stable operating history", "Consistent business deposits", "Acceptable credit and existing-debt profile"],
            "documents": ["Recent business bank statements", "Current P&L and balance sheet", "Business tax returns", "Debt schedule"],
            "exclusions": ["Long-term real estate acquisition", "Unverified or unstable operating cash flow", "Uses prohibited by the selected lender"],
        },
        "es": {
            "closing_timeline": "El plazo depende de la revision bancaria y un perfil operativo completo.",
            "uses": ["Capital de trabajo recurrente", "Inventario estacional", "Brechas operativas de corto plazo"],
            "best_fit": ["Negocios establecidos con necesidades repetidas de capital", "Empresas que buscan acceso reutilizable en vez de un solo desembolso"],
            "minimum_requirements": ["Historial operativo estable", "Depositos comerciales consistentes", "Perfil aceptable de credito y deuda existente"],
            "documents": ["Estados bancarios comerciales recientes", "P&L y balance actual", "Impuestos del negocio", "Calendario de deudas"],
            "exclusions": ["Compra de bienes raices a largo plazo", "Flujo operativo inestable o sin verificar", "Usos prohibidos por el prestamista seleccionado"],
        },
    },
    "term_loan_loc_hybrid": {
        "en": {
            "closing_timeline": "Structured review; timing varies with facility complexity.",
            "uses": ["Planned expansion", "Working-capital reserve", "Mixed fixed and recurring capital needs"],
            "best_fit": ["Established companies needing a funded term component and revolving availability", "Larger requests with a defined capital plan"],
            "minimum_requirements": ["Demonstrated repayment capacity", "Clear use-of-funds schedule", "Acceptable banking, credit, and leverage"],
            "documents": ["Business financial statements", "Tax returns", "Recent bank statements", "Debt schedule and use-of-funds detail"],
            "exclusions": ["Requests without a supportable repayment source", "Unresolved lien or leverage concerns", "Restricted industries or purposes"],
        },
        "es": {
            "closing_timeline": "Revision estructurada; el plazo varia segun la complejidad de la facilidad.",
            "uses": ["Expansion planificada", "Reserva de capital de trabajo", "Necesidades fijas y recurrentes combinadas"],
            "best_fit": ["Empresas establecidas que necesitan un componente a plazo y disponibilidad rotativa", "Solicitudes mayores con un plan de capital definido"],
            "minimum_requirements": ["Capacidad de pago demostrada", "Detalle claro del uso de fondos", "Banca, credito y apalancamiento aceptables"],
            "documents": ["Estados financieros del negocio", "Impuestos", "Estados bancarios recientes", "Calendario de deuda y detalle del uso"],
            "exclusions": ["Solicitudes sin fuente de pago sostenible", "Problemas de gravamen o apalancamiento sin resolver", "Industrias o usos restringidos"],
        },
    },
    "equipment_financing": {
        "en": {
            "closing_timeline": "Often driven by equipment documentation, valuation, and vendor readiness.",
            "uses": ["New or used equipment", "Commercial vehicles", "Machinery and technology assets"],
            "best_fit": ["Businesses purchasing an identifiable revenue-producing asset", "Operators preserving working capital while acquiring equipment"],
            "minimum_requirements": ["Eligible equipment and acceptable valuation", "Supportable payment capacity", "Qualified vendor and ownership structure"],
            "documents": ["Vendor quote or purchase invoice", "Equipment description and serial information", "Bank statements and financials", "Existing equipment debt details"],
            "exclusions": ["Unverifiable equipment", "Unsupported private-party transactions", "Assets or uses excluded by the selected lender"],
        },
        "es": {
            "closing_timeline": "Generalmente depende de los documentos del equipo, su valoracion y el vendedor.",
            "uses": ["Equipo nuevo o usado", "Vehiculos comerciales", "Maquinaria y activos tecnologicos"],
            "best_fit": ["Negocios que compran un activo identificable que produce ingresos", "Operadores que desean preservar capital de trabajo"],
            "minimum_requirements": ["Equipo elegible y valoracion aceptable", "Capacidad de pago sostenible", "Vendedor y estructura de propiedad calificados"],
            "documents": ["Cotizacion o factura del vendedor", "Descripcion y serie del equipo", "Estados bancarios y financieros", "Detalle de deuda actual del equipo"],
            "exclusions": ["Equipo no verificable", "Transacciones privadas sin soporte", "Activos o usos excluidos por el prestamista"],
        },
    },
    "jumbo_term_loan": {
        "en": {
            "closing_timeline": "Longer structured review for larger and more complex facilities.",
            "uses": ["Major expansion", "Acquisition or recapitalization", "Large strategic working-capital needs"],
            "best_fit": ["Established companies with meaningful scale", "Complex requests requiring a tailored lender process"],
            "minimum_requirements": ["Strong documented cash flow", "Experienced ownership and management", "Detailed transaction and repayment plan"],
            "documents": ["Multi-year financial statements and tax returns", "Interim financials", "Bank statements", "Debt schedule and transaction documents"],
            "exclusions": ["Insufficient documented repayment capacity", "Incomplete ownership or entity structure", "Transactions outside lender policy"],
        },
        "es": {
            "closing_timeline": "Revision estructurada mas extensa para facilidades grandes y complejas.",
            "uses": ["Expansion importante", "Adquisicion o recapitalizacion", "Necesidades estrategicas grandes de capital de trabajo"],
            "best_fit": ["Empresas establecidas de escala significativa", "Solicitudes complejas que necesitan un proceso personalizado"],
            "minimum_requirements": ["Flujo de caja documentado y solido", "Propiedad y gerencia con experiencia", "Plan detallado de transaccion y pago"],
            "documents": ["Financieros e impuestos de varios anos", "Financieros interinos", "Estados bancarios", "Calendario de deuda y documentos de transaccion"],
            "exclusions": ["Capacidad de pago documentada insuficiente", "Estructura de propiedad o entidades incompleta", "Transacciones fuera de politica"],
        },
    },
    "transportation_finance": {
        "en": {
            "closing_timeline": "Timing depends on fleet, equipment, insurance, and operating review.",
            "uses": ["Commercial vehicles and trailers", "Fleet expansion or replacement", "Transportation operating capital"],
            "best_fit": ["Transportation companies with documented contracts or operating revenue", "Operators with identifiable fleet needs"],
            "minimum_requirements": ["Acceptable operating history", "Supportable equipment value and payment", "Required licenses and insurance"],
            "documents": ["Equipment or fleet schedule", "Vendor quotes", "Bank statements and financials", "Insurance and operating authority"],
            "exclusions": ["Unlicensed operations", "Uninsurable or unverifiable equipment", "Uses outside transportation operations"],
        },
        "es": {
            "closing_timeline": "El plazo depende de la revision de flota, equipo, seguro y operaciones.",
            "uses": ["Vehiculos comerciales y remolques", "Expansion o reemplazo de flota", "Capital operativo de transporte"],
            "best_fit": ["Empresas de transporte con contratos o ingresos documentados", "Operadores con necesidades de flota identificables"],
            "minimum_requirements": ["Historial operativo aceptable", "Valor y pago del equipo sostenibles", "Licencias y seguros requeridos"],
            "documents": ["Calendario de equipo o flota", "Cotizaciones", "Estados bancarios y financieros", "Seguro y autoridad operativa"],
            "exclusions": ["Operaciones sin licencia", "Equipo no asegurable o no verificable", "Usos fuera de la operacion de transporte"],
        },
    },
    "sba": {
        "en": {
            "closing_timeline": "Government-backed underwriting requires a complete eligibility and closing package.",
            "uses": ["Business acquisition", "Expansion and working capital", "Eligible real estate or equipment", "Qualified debt refinance"],
            "best_fit": ["Eligible small businesses seeking longer terms", "Transactions with a clear business purpose and repayment plan"],
            "minimum_requirements": ["SBA-eligible business and owners", "Demonstrated repayment ability", "Required owner injection where applicable"],
            "documents": ["Business and personal tax returns", "Financial statements", "Ownership and entity documents", "Transaction-specific closing package"],
            "exclusions": ["SBA-ineligible businesses or uses", "Incomplete eligibility disclosures", "Transactions unable to meet lender or SBA requirements"],
        },
        "es": {
            "closing_timeline": "La suscripcion con respaldo gubernamental requiere un paquete completo.",
            "uses": ["Adquisicion de negocio", "Expansion y capital de trabajo", "Bienes raices o equipo elegible", "Refinanciamiento de deuda calificado"],
            "best_fit": ["Pequenos negocios elegibles que buscan plazos mas largos", "Transacciones con proposito y plan de pago claros"],
            "minimum_requirements": ["Negocio y propietarios elegibles bajo SBA", "Capacidad de pago demostrada", "Aporte requerido del propietario cuando aplique"],
            "documents": ["Impuestos comerciales y personales", "Estados financieros", "Documentos de propiedad y entidad", "Paquete de cierre especifico"],
            "exclusions": ["Negocios o usos no elegibles bajo SBA", "Declaraciones de elegibilidad incompletas", "Transacciones fuera de requisitos del prestamista o SBA"],
        },
    },
    "sba_grocery": {
        "en": {
            "closing_timeline": "SBA review plus operating, inventory, lease, and site diligence.",
            "uses": ["Grocery acquisition", "Store expansion or renovation", "Equipment, inventory, and eligible working capital"],
            "best_fit": ["Independent grocers and food-market operators", "Acquisitions with documented store economics"],
            "minimum_requirements": ["SBA eligibility", "Supportable store cash flow", "Acceptable lease or real estate structure"],
            "documents": ["Store financials and tax returns", "Lease or property documents", "Inventory and equipment schedules", "Purchase agreement when applicable"],
            "exclusions": ["Ineligible business models", "Unsupported inventory or sales assumptions", "Insufficient site or lease control"],
        },
        "es": {
            "closing_timeline": "Revision SBA mas diligencia de operacion, inventario, contrato y local.",
            "uses": ["Adquisicion de supermercado", "Expansion o renovacion", "Equipo, inventario y capital de trabajo elegible"],
            "best_fit": ["Supermercados independientes y mercados de alimentos", "Adquisiciones con economia operativa documentada"],
            "minimum_requirements": ["Elegibilidad SBA", "Flujo de caja sostenible de la tienda", "Contrato o estructura inmobiliaria aceptable"],
            "documents": ["Financieros e impuestos de la tienda", "Contrato o documentos de propiedad", "Calendarios de inventario y equipo", "Acuerdo de compra cuando aplique"],
            "exclusions": ["Modelos de negocio no elegibles", "Supuestos de inventario o ventas sin soporte", "Control insuficiente del local o contrato"],
        },
    },
    "sba_made_in_america": {
        "en": {
            "closing_timeline": "SBA review plus project, sourcing, equipment, and job-impact diligence.",
            "uses": ["Domestic manufacturing equipment", "Facility expansion", "Eligible production and working-capital investment"],
            "best_fit": ["U.S. manufacturers investing in domestic capacity", "Projects with a documented implementation budget"],
            "minimum_requirements": ["SBA and program eligibility", "Documented domestic business purpose", "Supportable projected repayment capacity"],
            "documents": ["Project budget and vendor quotes", "Business financials and tax returns", "Facility documents", "Ownership and sourcing support"],
            "exclusions": ["Unsupported project costs", "Ineligible foreign-use components", "Projects unable to document repayment or implementation"],
        },
        "es": {
            "closing_timeline": "Revision SBA mas diligencia del proyecto, proveedores, equipo e impacto laboral.",
            "uses": ["Equipo de manufactura nacional", "Expansion de instalaciones", "Inversion elegible en produccion y capital de trabajo"],
            "best_fit": ["Fabricantes de EE. UU. que invierten en capacidad nacional", "Proyectos con presupuesto de implementacion documentado"],
            "minimum_requirements": ["Elegibilidad SBA y del programa", "Proposito comercial nacional documentado", "Capacidad proyectada de pago sostenible"],
            "documents": ["Presupuesto y cotizaciones", "Financieros e impuestos", "Documentos de instalaciones", "Soporte de propiedad y proveedores"],
            "exclusions": ["Costos de proyecto sin soporte", "Componentes no elegibles", "Proyectos sin evidencia de pago o implementacion"],
        },
    },
})


_GENERIC_DETAIL_COPY = {
    "en": {
        "closing_timeline": "Timing and structure are confirmed after lender review.",
        "uses": ["Program-specific business purpose", "Qualified expansion or asset needs"],
        "best_fit": ["Businesses whose request matches the displayed amount and term", "Files ready for a structured funding consultation"],
        "minimum_requirements": ["Complete business and ownership profile", "Acceptable credit and banking evidence", "Program-specific cash-flow and industry review"],
        "documents": ["Business bank statements", "Business financials and tax returns", "Debt schedule", "Purpose-specific supporting documents"],
        "exclusions": ["Eligibility and industry restrictions vary by lender", "Final terms require underwriting and documentation"],
    },
    "es": {
        "closing_timeline": "El plazo y la estructura se confirman despues de la revision del prestamista.",
        "uses": ["Proposito comercial permitido por el programa", "Expansion o activos calificados"],
        "best_fit": ["Negocios cuya solicitud coincide con el monto y plazo mostrados", "Expedientes listos para una consulta de financiamiento"],
        "minimum_requirements": ["Perfil completo del negocio y propietarios", "Evidencia bancaria y de credito aceptable", "Revision de flujo de caja e industria"],
        "documents": ["Estados bancarios comerciales", "Financieros e impuestos del negocio", "Calendario de deudas", "Documentos relacionados con el uso solicitado"],
        "exclusions": ["Las restricciones varian segun el prestamista", "Los terminos finales requieren suscripcion y documentacion"],
    },
}


def _can_view_contact(user: User, contact: DealerRepContact) -> bool:
    return user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC} or contact.owner_user_id == user.id


async def _contact_access_filter(user: User):
    if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        return True
    return or_(
        DealerRepContact.owner_user_id == user.id,
        exists(select(DealerRepContactAssignment.id).where(
            DealerRepContactAssignment.contact_id == DealerRepContact.id,
            DealerRepContactAssignment.user_id == user.id,
        )),
    )


async def _load_contact(db: AsyncSession, user: User, contact_id: UUID) -> DealerRepContact:
    require_team_or_rep(user)
    row = (await db.execute(select(DealerRepContact).where(
        DealerRepContact.id == contact_id, await _contact_access_filter(user)
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return row


async def _next_case_ref(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"QC-{year}-"
    top = (await db.execute(select(func.max(DealerBusiness.case_ref)).where(
        DealerBusiness.case_ref.like(f"{prefix}%")
    ))).scalar_one_or_none()
    try:
        number = int(str(top).rsplit("-", 1)[1]) if top else 0
    except (IndexError, ValueError):
        number = 0
    return f"{prefix}{number + 1:05d}"


def _catalog_item(row: DealerProductCatalog, locale: str) -> dict:
    language = "es" if locale == "es" else "en"
    copy = (row.copy or {}).get(language) or (row.copy or {}).get("en") or {}
    disclosure = (row.disclosures or {}).get(language) or (row.disclosures or {}).get("en")
    pricing = (row.pricing or {}).get(language) or (row.pricing or {}).get("en")
    defaults = (_CATALOG_DETAILS.get(row.program_key) or {}).get(language) or _GENERIC_DETAIL_COPY[language]
    detail = {**defaults, **(copy.get("details") or {})}
    return {
        "id": str(row.id), "program_key": row.program_key, "version": row.version,
        "category": row.category, "name": copy.get("name", row.program_key.replace("_", " ").title()),
        "summary": copy.get("summary") or _CATALOG_SUMMARIES.get(row.program_key, {}).get(language), "highlights": copy.get("highlights", []),
        "pricing": pricing, "disclosure": disclosure,
        "amount_min": float(row.amount_min) if row.amount_min is not None else None,
        "amount_max": float(row.amount_max) if row.amount_max is not None else None,
        "term_min_months": row.term_min_months, "term_max_months": row.term_max_months,
        "effective_at": row.effective_at, "active": row.active,
        "details": detail,
        "direct_action": "start_application" if row.program_key in _DIRECT_APPLICATION_KEYS else "book_call",
        "icon_key": row.program_key,
    }


def _render_catalog_pdf(rows: list[DealerProductCatalog], locale: str) -> bytes:
    from weasyprint import HTML

    def bullet_list(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ul>"

    cards = ""
    for row in rows:
        item = _catalog_item(row, locale)
        details = item["details"]
        sections = [
            ("Uses" if locale == "en" else "Usos", details.get("uses") or []),
            ("Best fit" if locale == "en" else "Mejor perfil", details.get("best_fit") or []),
            ("Minimum requirements" if locale == "en" else "Requisitos minimos", details.get("minimum_requirements") or []),
            ("Expected documents" if locale == "en" else "Documentos esperados", details.get("documents") or []),
            ("Limitations" if locale == "en" else "Limitaciones", details.get("exclusions") or []),
        ]
        cards += (
            f"<section><h2>{html.escape(str(item['name']))}</h2>"
            f"<p>{html.escape(str(item.get('summary') or ''))}</p>"
            f"<div class='terms'><b>${float(row.amount_min or 0):,.0f}-${float(row.amount_max or 0):,.0f}</b>"
            f"<span>{html.escape(str(item.get('pricing') or 'Pricing subject to review'))}</span></div>"
            f"<p><b>{html.escape(str(details.get('closing_timeline') or ''))}</b></p>"
            + "".join(f"<h3>{title}</h3>{bullet_list(list(values))}" for title, values in sections)
            + f"<p class='disclosure'>{html.escape(str(item.get('disclosure') or ''))}</p></section>"
        )
    document_html = (
        "<style>@page{size:Letter;margin:.6in}body{font:13px Arial;color:#101828}"
        "h1{color:#174b84}h2{margin:0 0 6px}h3{font-size:11px;text-transform:uppercase;color:#526070;margin:12px 0 3px}"
        "section{border:1px solid #d8e0ea;padding:18px;margin:14px 0;border-radius:8px;page-break-inside:avoid}"
        ".terms{display:flex;justify-content:space-between;border-top:1px solid #e5e9ef;border-bottom:1px solid #e5e9ef;padding:9px 0}"
        "ul{margin:4px 0 0;padding-left:20px}li{margin:3px 0}.disclosure,small{color:#667085}</style>"
        f"<h1>{'Catálogo de financiamiento' if locale == 'es' else 'Funding program catalog'}</h1>"
        f"{cards}<small>{'Evaluación preliminar; no es un compromiso de préstamo.' if locale == 'es' else 'Preliminary fit only; not a commitment to lend.'}</small>"
    )
    return HTML(string=document_html).write_pdf()


@router.get("/products")
async def list_products(
    user: CurrentUser, db: AsyncSession = Depends(get_db), locale: str = Query("en", pattern="^(en|es)$"),
    q: str = Query("", max_length=120), category: str = Query("all", max_length=48),
) -> dict:
    require_team_or_rep(user)
    stmt = select(DealerProductCatalog).where(DealerProductCatalog.active.is_(True))
    if category != "all": stmt = stmt.where(DealerProductCatalog.category == category)
    rows = (await db.execute(stmt.order_by(DealerProductCatalog.sort_order, DealerProductCatalog.program_key))).scalars().all()
    items = [_catalog_item(row, locale) for row in rows]
    needle = q.strip().lower()
    if needle:
        items = [item for item in items if needle in f"{item['name']} {item['category']} {item.get('summary') or ''}".lower()]
    return {"items": items, "questions": [{**question, "label": question[locale]} for question in QUESTIONS]}


@router.get("/products/booking")
async def product_booking_target(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict:
    """The one approved escalation target for non-direct programs."""
    require_team_or_rep(user)
    owner = await primary_super_admin(db)
    booking = None
    if owner is not None:
        booking = (
            await db.execute(
                select(BookingSettings).where(
                    BookingSettings.user_id == owner.id,
                    BookingSettings.enabled.is_(True),
                    BookingSettings.slug.is_not(None),
                )
            )
        ).scalar_one_or_none()
    if booking is None or not booking.slug:
        return {"enabled": False, "url": None}
    base = get_settings().frontend_app_url.rstrip("/")
    return {
        "enabled": True,
        "url": f"{base}/book/{booking.slug}?source=field_desk_product&campaign=product_booklet",
    }


@router.get("/products/detail/{program_key}")
async def product_detail(
    program_key: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    locale: str = Query("en", pattern="^(en|es)$"),
) -> dict:
    require_team_or_rep(user)
    row = (
        await db.execute(
            select(DealerProductCatalog).where(
                DealerProductCatalog.program_key == program_key,
                DealerProductCatalog.active.is_(True),
            ).order_by(DealerProductCatalog.version.desc())
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    siblings = list(
        (
            await db.execute(
                select(DealerProductCatalog).where(
                    DealerProductCatalog.active.is_(True)
                ).order_by(DealerProductCatalog.sort_order, DealerProductCatalog.program_key)
            )
        ).scalars().all()
    )
    keys = [item.program_key for item in siblings]
    index = keys.index(row.program_key)
    return {
        "item": _catalog_item(row, locale),
        "position": index + 1,
        "total": len(keys),
        "previous_key": keys[index - 1] if index > 0 else keys[-1],
        "next_key": keys[index + 1] if index < len(keys) - 1 else keys[0],
    }


@router.patch("/products/{program_key}")
async def update_product(program_key: str, payload: ProductCatalogUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_super_admin(user)
    row = (await db.execute(select(DealerProductCatalog).where(
        DealerProductCatalog.program_key == program_key,
        DealerProductCatalog.active.is_(True),
    ).order_by(DealerProductCatalog.version.desc()))).scalars().first()
    if row is None: raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(row, key, value)
    row.updated_by_user_id = user.id
    await db.commit(); await db.refresh(row)
    return _catalog_item(row, "en")


async def _find_or_create_company_contact(db: AsyncSession, user: User, payload: CompanyContactIn) -> tuple[DealerRepCompany, DealerRepContact]:
    email = payload.email.strip().lower() if payload.email else None
    contact = None
    if email:
        contact = (await db.execute(select(DealerRepContact).where(
            DealerRepContact.owner_user_id == user.id, DealerRepContact.email == email
        ).order_by(DealerRepContact.updated_at.desc()))).scalars().first()
    company = await db.get(DealerRepCompany, contact.company_id) if contact and contact.company_id else None
    if company is None:
        company = (await db.execute(select(DealerRepCompany).where(
            DealerRepCompany.owner_user_id == user.id,
            func.lower(DealerRepCompany.name) == payload.company_name.strip().lower(),
        ).order_by(DealerRepCompany.updated_at.desc()))).scalars().first()
    if company is None:
        company = DealerRepCompany(owner_user_id=user.id, name=payload.company_name.strip(), industry=payload.industry,
            address=payload.address, city=payload.city, state=payload.state, zip=payload.zip)
        db.add(company); await db.flush()
    if contact is None:
        contact = DealerRepContact(owner_user_id=user.id, company_id=company.id, full_name=payload.contact_name.strip(),
            company=company.name, email=email, phone_e164=payload.phone, source="product_finder", last_activity_at=datetime.now(timezone.utc))
        db.add(contact); await db.flush()
    else:
        contact.company_id = contact.company_id or company.id
        contact.company = company.name; contact.phone_e164 = payload.phone or contact.phone_e164
        contact.last_activity_at = datetime.now(timezone.utc)
    return company, contact


@router.post("/product-finder/sessions", status_code=status.HTTP_201_CREATED)
async def create_finder_session(payload: CompanyContactIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_team_or_rep(user)
    company, contact = await _find_or_create_company_contact(db, user, payload)
    existing = (await db.execute(select(DealerProductFinderSession).where(
        DealerProductFinderSession.owner_user_id == user.id,
        DealerProductFinderSession.contact_id == contact.id,
        DealerProductFinderSession.status.in_(["screening", "draft"]),
    ).order_by(DealerProductFinderSession.updated_at.desc()))).scalars().first()
    if existing:
        return {"id": str(existing.id), "dealer_id": str(existing.dealer_id), "contact_id": str(contact.id),
            "company_id": str(company.id), "answers": existing.answers, "result": existing.current_result,
            "client_requested_amount": float(existing.client_requested_amount or 0), "reused": True}
    dealer = DealerBusiness(name=company.name, legal_name=company.name, email=contact.email, phone=contact.phone_e164,
        address=company.address, city=company.city, state=company.state, zip=company.zip, industry=payload.industry or "other",
        entity_type="unknown", funding_goal=Decimal(str(payload.requested_amount)),
        client_requested_amount=Decimal(str(payload.requested_amount)), funding_purpose="other",
        use_of_proceeds_note=payload.use_of_funds, owner_user_id=user.id, case_ref=await _next_case_ref(db),
        application_lifecycle="draft", status="draft")
    db.add(dealer); await db.flush()
    contact.dealer_id = contact.dealer_id or dealer.id
    db.add(DealerApplicationContact(dealer_id=dealer.id, contact_id=contact.id, relationship="prospect", is_primary=True))
    answers = {"requested_amount": payload.requested_amount, "use_of_funds": payload.use_of_funds, "industry": payload.industry}
    session = DealerProductFinderSession(owner_user_id=user.id, company_id=company.id, contact_id=contact.id,
        dealer_id=dealer.id, locale=payload.locale, answers=answers, client_requested_amount=Decimal(str(payload.requested_amount)))
    db.add(session); await db.commit(); await db.refresh(session)
    return {"id": str(session.id), "dealer_id": str(dealer.id), "contact_id": str(contact.id), "company_id": str(company.id),
        "answers": answers, "result": None, "client_requested_amount": payload.requested_amount, "reused": False}


async def _load_session(db: AsyncSession, user: User, session_id: UUID) -> DealerProductFinderSession:
    row = await db.get(DealerProductFinderSession, session_id)
    if row is None or (user.role == Role.FIELD_REP and row.owner_user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Screening session not found")
    require_team_or_rep(user); return row


@router.post("/product-finder/sessions/{session_id}/screen")
async def screen_session(session_id: UUID, payload: FinderAnswersIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    session = await _load_session(db, user, session_id)
    answers = {**(session.answers or {}), **payload.answers}
    result = screen_products(answers, session.locale)
    session.answers = answers; session.current_result = result
    session.recommended_amount = Decimal(str(result["recommended_amount"])) if result.get("recommended_amount") else None
    db.add(DealerProductScreeningSnapshot(session_id=session.id, source="self_reported", inputs=answers, result=result, created_by_user_id=user.id))
    await db.commit()
    return {"id": str(session.id), "answers": answers, "result": result}


@router.post("/product-finder/sessions/{session_id}/confirm-funding-goal")
async def confirm_funding_goal(session_id: UUID, payload: FundingGoalConfirmIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    session = await _load_session(db, user, session_id); dealer = await db.get(DealerBusiness, session.dealer_id)
    if dealer is None: raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft file not found")
    maximum = float(session.recommended_amount or 0)
    if maximum and payload.amount > maximum: raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Confirmed amount exceeds the screened maximum")
    dealer.funding_goal = Decimal(str(payload.amount)); session.funding_goal_confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"client_requested_amount": float(dealer.client_requested_amount or 0), "funding_goal": float(dealer.funding_goal or 0)}


@router.post("/product-finder/sessions/{session_id}/start-application")
async def start_application(session_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    session = await _load_session(db, user, session_id); dealer = await db.get(DealerBusiness, session.dealer_id)
    if dealer is None: raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft file not found")
    if dealer.application_lifecycle == "draft":
        dealer.application_lifecycle = "active"; dealer.status = "active"; session.status = "promoted"
        db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
        await propose_targets(db, dealer)
        try: await buckets_link.ensure_bucket(db, dealer)
        except Exception: pass
        if user.role == Role.FIELD_REP:
            db.add(DealerRepLead(dealer_id=dealer.id, rep_user_id=user.id, status="draft", status_history=[]))
        await db.commit()
    return {"dealer_id": str(dealer.id), "route": f"/applications/{dealer.id}?step=1"}


@router.get("/contacts")
async def list_contacts(user: CurrentUser, db: AsyncSession = Depends(get_db), q: str = Query("", max_length=160), limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)) -> dict:
    require_team_or_rep(user); filters = [await _contact_access_filter(user)]
    if q.strip():
        like = f"%{q.strip().lower()}%"; filters.append(or_(func.lower(DealerRepContact.full_name).like(like), func.lower(func.coalesce(DealerRepContact.company, "")).like(like), func.lower(func.coalesce(DealerRepContact.email, "")).like(like), func.lower(func.coalesce(DealerRepContact.phone_e164, "")).like(like)))
    total = int((await db.execute(select(func.count()).select_from(DealerRepContact).where(*filters))).scalar_one())
    rows = (await db.execute(select(DealerRepContact).where(*filters).order_by(DealerRepContact.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": str(row.id), "company_id": str(row.company_id) if row.company_id else None, "name": row.full_name, "company": row.company, "email": row.email, "phone": row.phone_e164, "source": row.source, "updated_at": row.updated_at} for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/companies")
async def list_companies(user: CurrentUser, db: AsyncSession = Depends(get_db), q: str = Query("", max_length=160), limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)) -> dict:
    require_team_or_rep(user)
    filters = [] if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC} else [or_(
        DealerRepCompany.owner_user_id == user.id,
        exists(select(DealerRepContactAssignment.id).join(
            DealerRepContact, DealerRepContact.id == DealerRepContactAssignment.contact_id
        ).where(DealerRepContact.company_id == DealerRepCompany.id, DealerRepContactAssignment.user_id == user.id)),
    )]
    if q.strip():
        like = f"%{q.strip().lower()}%"
        filters.append(or_(func.lower(DealerRepCompany.name).like(like), func.lower(func.coalesce(DealerRepCompany.address, "")).like(like), func.lower(func.coalesce(DealerRepCompany.city, "")).like(like)))
    total = int((await db.execute(select(func.count()).select_from(DealerRepCompany).where(*filters))).scalar_one())
    rows = (await db.execute(select(DealerRepCompany).where(*filters).order_by(DealerRepCompany.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": str(row.id), "name": row.name, "industry": row.industry, "address": row.address, "city": row.city, "state": row.state, "status": row.status, "updated_at": row.updated_at} for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/companies/{company_id}")
async def company_detail(company_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_team_or_rep(user)
    company = await db.get(DealerRepCompany, company_id)
    shared = False
    if company is not None and user.role == Role.FIELD_REP:
        shared = bool((await db.execute(select(exists(select(DealerRepContactAssignment.id).join(
            DealerRepContact, DealerRepContact.id == DealerRepContactAssignment.contact_id
        ).where(DealerRepContact.company_id == company.id, DealerRepContactAssignment.user_id == user.id))))).scalar_one())
    if company is None or (user.role == Role.FIELD_REP and company.owner_user_id != user.id and not shared):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    contacts = (await db.execute(select(DealerRepContact).where(DealerRepContact.company_id == company.id).order_by(DealerRepContact.updated_at.desc()))).scalars().all()
    return {"id": str(company.id), "name": company.name, "industry": company.industry, "address": company.address, "city": company.city, "state": company.state, "zip": company.zip, "contacts": [{"id": str(row.id), "name": row.full_name, "email": row.email, "phone": row.phone_e164} for row in contacts]}


@router.get("/contacts/{contact_id}")
async def contact_detail(contact_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    contact = await _load_contact(db, user, contact_id)
    applications = (await db.execute(select(DealerBusiness).join(DealerApplicationContact, DealerApplicationContact.dealer_id == DealerBusiness.id).where(DealerApplicationContact.contact_id == contact.id).order_by(DealerBusiness.updated_at.desc()))).scalars().all()
    sessions = (await db.execute(select(DealerProductFinderSession).where(DealerProductFinderSession.contact_id == contact.id).order_by(DealerProductFinderSession.updated_at.desc()))).scalars().all()
    presentations = (await db.execute(select(DealerProductPresentation).where(DealerProductPresentation.contact_id == contact.id).order_by(DealerProductPresentation.created_at.desc()))).scalars().all()
    threads = (await db.execute(select(DealerRepInboxThread).where(DealerRepInboxThread.contact_id == contact.id).order_by(DealerRepInboxThread.updated_at.desc()))).scalars().all()
    return {"id": str(contact.id), "name": contact.full_name, "company": contact.company, "email": contact.email, "phone": contact.phone_e164,
        "applications": [{"id": str(row.id), "name": row.name, "case_ref": row.case_ref, "lifecycle": row.application_lifecycle, "status": row.status, "funding_goal": float(row.funding_goal or 0), "updated_at": row.updated_at} for row in applications],
        "sessions": [{"id": str(row.id), "status": row.status, "result": row.current_result, "updated_at": row.updated_at} for row in sessions],
        "presentations": [{"id": str(row.id), "program_keys": row.program_keys, "catalog_versions": row.catalog_versions, "pdf_sha256": row.pdf_sha256, "locale": row.locale, "channel": row.channel, "status": row.delivery_status, "created_at": row.created_at} for row in presentations],
        "threads": [{"id": str(row.id), "subject": row.subject, "channel": row.channel, "unread_count": row.unread_count, "updated_at": row.updated_at} for row in threads]}


@router.post("/contacts/{contact_id}/assignments", status_code=status.HTTP_201_CREATED)
async def assign_contact(contact_id: UUID, payload: ContactAssignmentIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    contact = await _load_contact(db, user, contact_id)
    if user.role == Role.FIELD_REP and contact.owner_user_id != user.id: raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owning rep can share this contact")
    existing = (await db.execute(select(DealerRepContactAssignment).where(DealerRepContactAssignment.contact_id == contact.id, DealerRepContactAssignment.user_id == payload.user_id))).scalar_one_or_none()
    if existing is None: db.add(DealerRepContactAssignment(contact_id=contact.id, user_id=payload.user_id, assigned_by_user_id=user.id)); await db.commit()
    return {"assigned": True}


def _normalize_subject(subject: str) -> str:
    value = subject.strip().lower()
    while re.match(r"^(re|fw|fwd)\s*:", value): value = re.sub(r"^(re|fw|fwd)\s*:\s*", "", value)
    return " ".join(value.split())[:200]


async def _presentation_contact(
    db: AsyncSession, user: User, payload: ProductPresentationIn
) -> DealerRepContact:
    if payload.contact_id is not None:
        contact = await _load_contact(db, user, payload.contact_id)
    else:
        first = (payload.first_name or "").strip()
        last = (payload.last_name or "").strip()
        email = (payload.email or "").strip().lower() or None
        phone = consent_delivery.normalize_phone(payload.phone)
        if not first or not last:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "First and last name are required for a new recipient",
            )
        if not email and not phone:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Add an email address or phone number",
            )
        matches = []
        if email:
            matches.append(func.lower(func.coalesce(DealerRepContact.email, "")) == email)
        if phone:
            matches.append(DealerRepContact.phone_e164 == phone)
        contact = (
            await db.execute(
                select(DealerRepContact).where(
                    DealerRepContact.owner_user_id == user.id,
                    or_(*matches),
                ).order_by(DealerRepContact.updated_at.desc())
            )
        ).scalars().first()
        if contact is None:
            contact = DealerRepContact(
                owner_user_id=user.id,
                full_name=f"{first} {last}".strip(),
                email=email,
                phone_e164=phone,
                source="product_presentation",
                last_activity_at=datetime.now(timezone.utc),
            )
            db.add(contact)
            await db.flush()
        else:
            contact.full_name = contact.full_name or f"{first} {last}".strip()
            contact.email = email or contact.email
            contact.phone_e164 = phone or contact.phone_e164
            contact.last_activity_at = datetime.now(timezone.utc)
    if payload.sms_transactional_consent:
        contact.sms_transactional_consented_at = datetime.now(timezone.utc)
    return contact


def _artifact_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/product-presentations", status_code=status.HTTP_201_CREATED)
async def present_products(payload: ProductPresentationIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_team_or_rep(user)
    contact = await _presentation_contact(db, user, payload)
    subject = payload.subject or ("Opciones de financiamiento" if payload.locale == "es" else "Funding options")
    catalog_rows = list((await db.execute(select(DealerProductCatalog).where(
        DealerProductCatalog.program_key.in_(payload.program_keys),
        DealerProductCatalog.active.is_(True),
    ).order_by(DealerProductCatalog.sort_order))).scalars().all())
    if not catalog_rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select at least one active program")
    pdf = await run_in_threadpool(_render_catalog_pdf, catalog_rows, payload.locale)
    thread = None
    delivery = "presented"
    provider_detail = ""
    sms_result = None
    if payload.channel in {"email", "sms"}:
        if payload.channel == "email" and not contact.email: raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Contact email is required")
        if payload.channel == "sms" and not contact.phone_e164: raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Contact phone is required")
        if payload.channel == "sms" and not payload.sms_transactional_consent:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Affirmative transactional SMS consent is required before texting",
            )
        filters = [DealerRepInboxThread.contact_id == contact.id, DealerRepInboxThread.channel == payload.channel, DealerRepInboxThread.status == "open"]
        if payload.channel == "email": filters.append(DealerRepInboxThread.subject_key == _normalize_subject(subject))
        thread = (await db.execute(select(DealerRepInboxThread).where(*filters).order_by(DealerRepInboxThread.updated_at.desc()))).scalars().first()
        if thread is None:
            thread = DealerRepInboxThread(owner_user_id=contact.owner_user_id, contact_id=contact.id, dealer_id=contact.dealer_id, subject=subject, subject_key=_normalize_subject(subject) if payload.channel == "email" else None, channel=payload.channel, source="product_presentation", last_message_at=datetime.now(timezone.utc)); db.add(thread); await db.flush()
        body = payload.message or ("Attached are the funding options we discussed." if payload.locale == "en" else "Adjuntamos las opciones de financiamiento que conversamos.")
        if payload.channel == "email":
            result = await run_in_threadpool(
                ses_client.send_raw_email,
                to_emails=[contact.email], subject=subject, body_text=body,
                attachments=[(f"qc-funding-options-{payload.locale}.pdf", pdf, "application/pdf")],
            )
            delivery = "sent" if result.ok else "failed"
            provider_detail = result.message_id or result.detail or delivery
    row = DealerProductPresentation(owner_user_id=user.id, company_id=contact.company_id, contact_id=contact.id, dealer_id=contact.dealer_id, session_id=payload.session_id, program_keys=payload.program_keys, locale=payload.locale, channel=payload.channel, delivery_status=delivery, inbox_thread_id=thread.id if thread else None, catalog_versions={item.program_key: item.version for item in catalog_rows}, pdf_sha256=hashlib.sha256(pdf).hexdigest())
    db.add(row)
    await db.flush()

    secure_url = None
    if payload.channel == "sms":
        token = secrets.token_urlsafe(32)
        filename = f"qc-funding-options-{payload.locale}.pdf"
        key = f"dealer-os/product-presentations/{row.id}/{uuid.uuid4()}-{filename}"
        stored = await run_in_threadpool(storage.put_bytes, key, pdf, "application/pdf")
        if not stored:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Secure PDF storage is unavailable; send the presentation by email instead",
            )
        artifact = DealerProductPresentationArtifact(
            presentation_id=row.id,
            s3_key=key,
            filename=filename,
            sha256=hashlib.sha256(pdf).hexdigest(),
            token_hash=_artifact_token_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(artifact)
        await db.flush()
        base = get_settings().public_api_url.rstrip("/")
        secure_url = f"{base}/api/v1/dealer-os/public/product-presentations/{token}"
        sms_body = payload.message or (
            f"Qualified Commercial funding options: {secure_url} Link expires in 7 days."
            if payload.locale == "en"
            else f"Opciones de financiamiento de Qualified Commercial: {secure_url} El enlace vence en 7 dias."
        )
        sms_result = await run_in_threadpool(
            consent_delivery._send_sms,  # noqa: SLF001 - shared production SMS adapter.
            contact.phone_e164,
            sms_body,
        )
        delivery = "sent" if sms_result.ok else "failed"
        provider_detail = sms_result.detail
        row.delivery_status = delivery
        body = sms_body

    if thread is not None:
        db.add(DealerRepInboxMessage(
            thread_id=thread.id,
            owner_user_id=contact.owner_user_id,
            contact_id=contact.id,
            dealer_id=contact.dealer_id,
            direction="outbound",
            channel=payload.channel,
            subject=subject if payload.channel == "email" else None,
            body=body,
            delivery_status=delivery,
            provider=sms_result.provider if sms_result is not None else "ses",
            provider_message_id=(
                sms_result.provider_message_id
                if sms_result is not None
                else provider_detail[:160] or None
            ),
            sender=sms_result.sender if sms_result is not None else user.email,
            recipient=contact.email if payload.channel == "email" else contact.phone_e164,
            read_at=datetime.now(timezone.utc),
        ))
        thread.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "contact_id": str(contact.id),
        "delivery_status": delivery,
        "delivery_detail": provider_detail,
        "secure_url": secure_url,
        "thread_id": str(thread.id) if thread else None,
    }


@router.get("/public/product-presentations/{token}", include_in_schema=False)
async def public_product_presentation(
    token: str, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    artifact = (
        await db.execute(
            select(DealerProductPresentationArtifact).where(
                DealerProductPresentationArtifact.token_hash == _artifact_token_hash(token)
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if artifact is None or artifact.expires_at <= now:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This presentation link is no longer available")
    url = storage.presign_get(
        artifact.s3_key,
        ttl=300,
        disposition=f'attachment; filename="{storage.safe_filename(artifact.filename)}"',
        content_type="application/pdf",
    )
    if not url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Presentation download is unavailable")
    artifact.download_count += 1
    artifact.last_downloaded_at = now
    await db.commit()
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/products/pdf")
async def products_pdf(user: CurrentUser, db: AsyncSession = Depends(get_db), keys: str = Query(...), locale: str = Query("en", pattern="^(en|es)$")):
    require_team_or_rep(user); wanted = [key for key in keys.split(",") if key][:12]
    rows = (await db.execute(select(DealerProductCatalog).where(DealerProductCatalog.program_key.in_(wanted), DealerProductCatalog.active.is_(True)).order_by(DealerProductCatalog.sort_order))).scalars().all()
    try:
        content = await run_in_threadpool(_render_catalog_pdf, list(rows), locale)
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PDF generation unavailable: {exc}") from exc
    return StreamingResponse(io.BytesIO(content), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="qc-product-catalog-{locale}.pdf"'})
