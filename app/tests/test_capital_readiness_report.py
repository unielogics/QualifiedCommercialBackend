from app.dealer_os.services.report_pdf import build_html


def _readiness(locale="en", coverage=80):
    return {
        "communication_locale": locale,
        "score": 87,
        "band": "ready_soon",
        "review_status": "provisional",
        "evidence_coverage_pct": coverage,
        "confidence_pct": 75,
        "snapshot_version": 3,
        "policy_key": "qc_lending_margin_v1",
        "policy_version": 1,
        "metrics": [
            {"key": "gross_margin_pct", "label": "Gross margin", "value": 18, "unit": "%", "status": "very_strong"},
        ],
        "blockers": [],
        "phases": [],
    }


def test_report_renders_canonical_version_and_provisional_status():
    document = build_html({"dealer": {"name": "Example"}, "capital_readiness": _readiness()})
    assert "Capital Readiness" in document
    assert "<b>87</b>" in document
    assert "18.0%" in document
    assert "Version 3" in document
    assert "Provisional — pending QC review" in document
    assert "Approval and lender terms require an authorized underwriting decision" in document


def test_report_hides_score_below_weighted_evidence_threshold():
    document = build_html({"dealer": {"name": "Example"}, "capital_readiness": _readiness(coverage=59.99)})
    assert "<b>87</b>" not in document
    assert "<b>—</b>" in document


def test_report_honors_file_language_and_escapes_source_text():
    readiness = _readiness(locale="es")
    readiness["metrics"][0]["label"] = "Margen bruto"
    document = build_html({"dealer": {"name": "<script>test</script>"}, "capital_readiness": readiness})
    assert '<html lang="es">' in document
    assert "Preparación de capital" in document
    assert "Provisional — pendiente de revisión de QC" in document
    assert "No hay períodos financieros normalizados" in document
    assert "No normalized financial periods" not in document
    assert "&lt;script&gt;test&lt;/script&gt;" in document
    assert "<script>test</script>" not in document


def test_property_report_omits_not_applicable_operating_margin():
    readiness = _readiness()
    readiness["metrics"][0]["source"] = {"not_applicable": True}
    readiness["metrics"].append({"label": "Property NOI", "value": 120000, "unit": "currency", "status": "healthy", "source": {"currency": "USD"}})
    document = build_html({"dealer": {"name": "Example"}, "capital_readiness": readiness})
    assert "Gross margin" not in document
    assert "Property NOI" in document
    assert "$120,000" in document
