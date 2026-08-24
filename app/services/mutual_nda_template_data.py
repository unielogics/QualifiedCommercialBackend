"""Versioned source for the public Mutual NDA / Non-Circumvention Agreement.

The substantive clauses are transcribed from the supplied governing text.
The only editorial change is removal of the fixed "page 3 of 3" reference.
"""

MUTUAL_NDA_TEMPLATE_DATA = {
    "notice": (
        "Please review the complete populated agreement and Exhibit A before signing. "
        "Do not include account numbers, passwords, routing numbers, or other banking credentials."
    ),
    "preamble": [
        "MUTUAL NONDISCLOSURE & NON-CIRCUMVENTION AGREEMENT",
        (
            'THIS NONDISCLOSURE & NON-CIRCUMVENTION AGREEMENT ("Agreement") is made and entered into '
            "on $effective_date by and between Qualified Commercial LLC and $counterparty_legal_name, "
            'as listed in the signature section of this Agreement (hereinafter individually referred to as "Party" '
            'and collectively as "Parties").'
        ),
    ],
    "sections": [
        {
            "heading": "1. Purpose.",
            "paragraphs": [
                "The Parties wish to explore business opportunities of mutual interest and in connection with these opportunities, each party may disclose to the other certain confidential business information which the disclosing party desires the receiving party to treat as confidential."
            ],
        },
        {
            "heading": "2. Confidential Information.",
            "paragraphs": [
                '"Confidential Information" means any information disclosed by either party to the other, either directly or indirectly in writing, orally or by inspection of tangible objects, including without limitation information relating to any business strategies or arrangements, intellectual property, proprietary information, including but not limited to, research, products, services, customer lists and customers, partnerships, business contacts (including, but not limited to names, addresses, telephone or telex numbers, e-mail addresses, etc.), or other business information. For the avoidance of doubt, business contacts strictly include third-party financial relations, hedge funds, family offices, and private credit sources. Confidential Information may also include information disclosed to a disclosing Party by third parties. Information communicated orally shall be considered Confidential Information if such information is designated as being confidential or proprietary within ninety (90) calendar days after the initial disclosure. Confidential Information shall not, however, include any information which (i) was publicly known and made generally available in the public domain prior to the time of disclosure by the disclosing party; (ii) becomes publicly known and made generally available in the public domain after disclosure by the disclosing party to the receiving party through no action or inaction of the receiving party or the receiving party\'s agents or employees; (iii) is already in the possession of the receiving party at the time of disclosure, without confidentiality restrictions, by the disclosing party as shown by the receiving party\'s files and records immediately prior to the time of disclosure; or (iv) is obtained by the receiving party from a third party without a breach of such third party\'s obligations of confidentiality. Confidential Information will be identified, in writing, via letters, faxes or email messages.',
                "For purposes of interpreting the foregoing exceptions to the nondisclosure and no-use obligations set forth in this Agreement, the parties agree that the Confidential Information which constitutes a compilation, assemblage or arrangement of information shall not be deemed to be within such exceptions merely because some or all of the components of the information therein are or become available to the public.",
            ],
        },
        {
            "heading": "3. Non-use and Nondisclosure.",
            "paragraphs": [
                "Each party agrees not to use any Confidential Information of the other party for any purpose except to evaluate and engage in discussions with the other party concerning a potential business relationship between the parties. Each party agrees not to disclose any Confidential Information of the other party to third parties or to such party's employees or professional advisors, except, subject to Section 4 of this Agreement, to those employees and professional advisors of the receiving party who are required to have the information to evaluate or engage in discussions concerning the contemplated business relationship. If either party or their respective directors, officers, employees, consultants or agents are requested or required by legal process to disclose any of the Confidential Information of the other party, the party required to make such disclosure shall give prompt notice to the other party so that such other party may seek a protective order or other appropriate relief. If a protective order or other relief is not obtained, the party required to make such disclosure shall disclose only that portion of the Confidential Information which its counsel advises that it is legally required to disclose."
            ],
        },
        {
            "heading": "4. Maintenance of Confidentiality.",
            "paragraphs": [
                "Each party agrees that it shall take reasonable measures to protect the secrecy of and avoid disclosure and unauthorized use of the Confidential Information of the other party. Without limiting the foregoing, each party shall take at least those measures that it takes to protect its own most highly confidential information and shall ensure that its employees and professional advisors who have access to Confidential Information of the other party (a) have signed a non-use and nondisclosure agreement in content similar to the provisions hereof, prior to any disclosure of Confidential Information to such employees or professional advisors, or (b) are advised of the confidential nature of the Confidential Information and the terms of this Agreement and are bound by a legally enforceable code of professional responsibility to protect the confidentiality of such Confidential Information. Neither party shall make any copies of the Confidential Information of the other party unless the same are previously approved in writing by the other party. Each party shall reproduce the other party's proprietary rights notices on any such approved copies, in the same manner in which such notices were set forth in or on the original."
            ],
        },
        {
            "heading": "5. No Obligation.",
            "paragraphs": [
                "Nothing herein shall obligate either party to proceed with any transaction between them, and each party reserves the right, in its sole discretion, to terminate the discussions contemplated by this Agreement concerning the business opportunity."
            ],
        },
        {
            "heading": "6. No Warranty.",
            "paragraphs": [
                'ALL CONFIDENTIAL INFORMATION IS PROVIDED "AS IS". EACH PARTY MAKES NO WARRANTIES, EXPRESS, IMPLIED OR OTHERWISE, REGARDING ITS ACCURACY, COMPLETENESS OR PERFORMANCE.'
            ],
        },
        {
            "heading": "7. Return of Materials.",
            "paragraphs": [
                "All documents and other tangible objects containing or representing Confidential Information which have been disclosed by either party to the other party, and all copies thereof which are in the possession of the other party, shall be and remain the property of the disclosing party and shall be promptly returned to the disclosing party upon the disclosing party's written request."
            ],
        },
        {
            "heading": "8. No License.",
            "paragraphs": [
                "Nothing in this Agreement is intended to grant any rights to either party in or to the Confidential Information of the other party except as expressly set forth herein."
            ],
        },
        {
            "heading": "9. Term.",
            "paragraphs": [
                "The obligations of each party hereunder shall survive in perpetuity or until such time as all Confidential Information of the other party disclosed hereunder becomes publicly known and made generally available in the public domain through no action or inaction of the receiving party or that receiving party's employees or agents."
            ],
        },
        {
            "heading": "10. Remedies.",
            "paragraphs": [
                "Each party recognizes that nothing in this Agreement is intended to limit any remedy of the other party and that such party could face possible criminal and civil actions, resulting in substantial monetary liability if such party misappropriates the other party's Confidential Information. In addition, each party recognizes that a violation of this Agreement could cause the other party irreparable harm, the amount of which may be extremely difficult to estimate, thus, making any remedy at law inadequate. Therefore, each party agrees that the other party shall have the right to apply to any court of competent jurisdiction for an order restraining any breach or threatened breach of this Agreement and for any other relief the non-breaching party deems appropriate without being required to post any bond or other security."
            ],
        },
        {
            "heading": "11. Non-circumvention:",
            "paragraphs": [
                "Notwithstanding anything to the contrary in this Agreement, each party to this Agreement agrees for itself and its affiliates and related parties that it will not engage in any transaction or disclose any Confidential Information that will interfere with, or deprive parties of the business opportunities disclosed pursuant to this Agreement. Also, the parties shall not in any manner solicit nor accept any business from sources or their affiliates that are directly or indirectly introduced by the other party or parties who directly introduced the source. Specifically, if a party is disclosed pursuant to this Agreement, the receiving party shall not attempt to close any business deal, secure funding, or circumvent the disclosing party without the disclosing party's express written consent and processing. Exceptions and Exclusions: This non-circumvention provision shall not apply to any main banks, institutions, or entities that the receiving party explicitly names and discloses as a pre-existing relationship prior to the execution of this Agreement. If a connection was demonstrably established and disclosed prior to this Agreement being signed, it is exempt from these restrictions."
            ],
        },
        {
            "heading": "12. Indemnification:",
            "paragraphs": [
                'Both parties to the Agreement will indemnify and hold each other harmless against any and all losses, claims, damages or liabilities (a "Claim"), including reasonable attorney\'s fees and expenses, which either party may incur in connection with or as a result of any actions taken under this Agreement, except to the extent that such Claim results from the gross negligence, intentional misconduct, or bad faith of the offending party performing such actions. This indemnification provision shall survive the termination of this Agreement.'
            ],
        },
        {
            "heading": "13. Severability.",
            "paragraphs": [
                "If any provision of this Agreement shall be held by a court of competent jurisdiction to be illegal, invalid or unenforceable, the remaining provisions shall remain in full force and effect. Should any of the obligations of this Agreement be found illegal or unenforceable as being too broad with respect to the duration, scope or subject matter thereof, such obligations shall be deemed and construed to be reduced to the maximum duration, scope or subject matter allowable by law."
            ],
        },
        {
            "heading": "14. Applicable Law.",
            "paragraphs": [
                "This Agreement shall be construed and governed by the laws of the State of New Jersey. If any action at law or in equity is necessary to enforce or interpret the rights arising out of or relating to this Agreement, the prevailing party shall be entitled to recover reasonable attorney's fees, costs and necessary disbursements in addition to any other relief to which it may be entitled."
            ],
        },
        {
            "heading": "15. Miscellaneous.",
            "paragraphs": [
                "This Agreement shall bind and inure to the benefit of the parties hereto and their successors and assigns. This document contains the entire agreement between the parties with respect to the subject matter hereof, and neither party shall have any obligation, express or implied by law, with respect to proprietary information of the other party except as set forth herein. Any failure to enforce any provision of this Agreement shall not constitute a waiver thereof or of any other provision. This Agreement may not be amended, nor any obligation waived, except by a writing signed by both parties hereto. If any claim is made by any party hereto relating to any conflict, omission or ambiguity in this Agreement, no presumption or burden of proof or persuasion shall be implied. This Agreement is not intended to limit any rights that the parties may have under trade secret, copyright, patent or other laws that may apply to the subject matter of this Agreement both during and after the term of this Agreement."
            ],
        },
        {
            "heading": "16. Signing Authority.",
            "paragraphs": [
                "Each of the individuals signing below warrants that such individual has the authority to sign for and on behalf of the respective parties."
            ],
        },
        {
            "heading": "EXHIBIT A - PRE-EXISTING RELATIONSHIPS",
            "paragraphs": [
                "$preexisting_relationship_declaration",
                "Do not include account numbers, routing numbers, passwords, access credentials, or other sensitive banking information.",
            ],
        },
        {
            "heading": "DISCLOSED RELATIONSHIPS",
            "paragraphs": [],
            "disclosure_field": "preexisting_relationship_rows",
        },
        {
            "heading": "SIGNATURES",
            "paragraphs": [
                "Qualified Commercial LLC",
                "By: $qc_signatory_name",
                "Name: Jonathan Franco",
                "Title: Authorized Executive",
                "Date: $qc_signature_date",
                "Contact: support@qualifiedcommercial.com",
                "$counterparty_legal_name",
                "By: $counterparty_signer_name",
                "Name: $counterparty_signer_name",
                "Title: $counterparty_signer_title",
                "Date: $counterparty_signature_date",
                "Email: $counterparty_signer_email",
            ],
        },
    ],
    "field_schema": {
        "effective_date": {"label": "Effective date", "field_type": "date", "default": ""},
        "counterparty_legal_name": {"label": "Counterparty legal name", "field_type": "text", "default": ""},
        "counterparty_entity_type": {"label": "Entity type", "field_type": "text", "default": ""},
        "counterparty_state_of_formation": {"label": "State of formation", "field_type": "text", "default": ""},
        "counterparty_principal_address": {"label": "Principal business address", "field_type": "address", "default": ""},
        "counterparty_signer_name": {"label": "Signer legal name", "field_type": "text", "default": ""},
        "counterparty_signer_title": {"label": "Signer title", "field_type": "text", "default": ""},
        "counterparty_signer_email": {"label": "Signer email", "field_type": "text", "default": ""},
        "counterparty_signature_date": {"label": "Counterparty signature date", "field_type": "date", "default": ""},
        "qc_signatory_name": {"label": "QC signatory", "field_type": "text", "default": "Jonathan Franco"},
        "qc_signature_date": {"label": "QC signature date", "field_type": "date", "default": ""},
        "preexisting_relationship_declaration": {"label": "Exhibit A declaration", "field_type": "text", "default": ""},
        "preexisting_relationship_rows": {
            "label": "Pre-existing relationships",
            "field_type": "disclosure_rows",
            "default": "",
            "table_columns": [
                {"key": "name", "label": "Institution or entity", "input_type": "text"},
                {"key": "category", "label": "Category", "input_type": "select", "options": ["Bank", "Financial institution", "Capital source", "Business entity", "Other"]},
                {"key": "description", "label": "Relationship description", "input_type": "text"},
                {"key": "start_date", "label": "Approximate start date", "input_type": "date"},
            ],
        },
    },
}
