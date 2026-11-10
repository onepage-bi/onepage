"""
SAML2 SP helpers — ported from Price Tracker, env prefix PT_ → OP_.
"""
from __future__ import annotations

from typing import Optional

try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    SAML_AVAILABLE = True
except ImportError:
    SAML_AVAILABLE = False

from app.auth import samlreplay
from app.config import settings


def _saml_settings() -> dict:
    base = settings.saml_sp_base_url.rstrip("/")
    return {
        "strict": settings.saml_strict,
        "debug": False,
        "sp": {
            "entityId": f"{base}/saml/metadata",
            "assertionConsumerService": {
                "url": f"{base}/saml/acs",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
            "x509cert": "",
            "privateKey": "",
        },
        "idp": {
            "entityId": settings.saml_idp_entity_id,
            "singleSignOnService": {
                "url": settings.saml_idp_sso_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": settings.saml_idp_cert,
        },
        "security": {
            # Outgoing AuthnRequests are not signed: signing them needs an SP
            # key pair, and this SP has none (x509cert/privateKey above are
            # deliberately empty). ADFS does not require it. Left explicit so
            # the next reader sees a decision rather than an oversight.
            "authnRequestsSigned": False,
            # THE flag that authenticates a login. With this false the SP
            # accepts an unsigned assertion, and OP_SAML_STRICT does not help —
            # it only enables python3-saml's strict mode. Anyone who could POST
            # to /saml/acs would sign in as any person_id.
            "wantAssertionsSigned": settings.saml_want_assertions_signed,
            # Response-level signing is not required separately: ADFS signs the
            # assertion, and wantAssertionsSigned above already establishes
            # authenticity. Demanding both is a common way to break a working
            # trust for no added security.
            "wantMessagesSigned": False,
            "wantAttributeStatement": False,
            # Do NOT emit <samlp:RequestedAuthnContext>. python3-saml otherwise
            # defaults it to PasswordProtectedTransport, which ADFS reads as a
            # demand for forced password auth and refuses seamless Windows
            # Integrated Auth (Kerberos). Setting this to False drops the element
            # so ADFS picks the method itself (Kerberos/WIA in the intranet zone).
            "requestedAuthnContext": False,
            "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
        },
    }


def get_saml_redirect_url(request_dict: dict) -> tuple[str, str | None]:
    """(redirect url, id of the AuthnRequest we just built).

    The caller keeps that id and hands it back at the ACS so the response can be
    tied to the attempt that asked for it — see process_saml_response.
    """
    if not SAML_AVAILABLE:
        raise RuntimeError("python3-saml is not installed")
    auth = OneLogin_Saml2_Auth(request_dict, _saml_settings())
    url = auth.login()
    return url, auth.get_last_request_id()


def process_saml_response(request_dict: dict, request_id: str | None = None) -> dict:
    """Returns {"person_id": str}. Raises ValueError on failure.

    `request_id` binds the response to the login attempt that started it.
    python3-saml only compares it when the IdP echoed an InResponseTo
    (`if in_response_to is not None and request_id is not None`), so passing it
    tightens the SP-initiated flow — the normal one here — without refusing an
    IdP-initiated assertion that legitimately carries none. Passing None, as
    this did before, meant the check could never run at all.
    """
    if not SAML_AVAILABLE:
        raise RuntimeError("python3-saml is not installed")

    auth = OneLogin_Saml2_Auth(request_dict, _saml_settings())
    auth.process_response(request_id=request_id)

    errors = auth.get_errors()
    if errors:
        raise ValueError(f"SAML error: {errors}; {auth.get_last_error_reason()}")
    if not auth.is_authenticated():
        raise ValueError("SAML authentication failed")

    # Signature and time window are checked above; neither makes the assertion
    # single-use. Refuse a second presentation of the same one.
    if not samlreplay.claim(auth.get_last_assertion_id()):
        raise ValueError("SAML assertion has already been used")

    attrs = auth.get_attributes()
    name_id = auth.get_nameid()
    claim = settings.saml_claim_person_id

    person_id: Optional[str] = None
    if claim in attrs and attrs[claim]:
        person_id = str(attrs[claim][0]).strip()
    if not person_id and name_id:
        person_id = str(name_id).strip()

    if not person_id:
        raise ValueError(
            f"Cannot extract person_id. Available attributes: {list(attrs.keys())}"
        )
    return {"person_id": person_id}


def get_sp_metadata() -> str:
    if SAML_AVAILABLE:
        try:
            s = OneLogin_Saml2_Settings(settings=_saml_settings(), sp_validation_only=True)
            return s.get_sp_metadata()
        except Exception:
            pass

    # Minimal fallback
    cfg = _saml_settings()
    entity_id = cfg["sp"]["entityId"]
    acs_url = cfg["sp"]["assertionConsumerService"]["url"]
    return (
        '<?xml version="1.0"?>\n'
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{entity_id}">\n'
        '  <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="false" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        f'    <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{acs_url}" index="1"/>\n'
        '  </md:SPSSODescriptor>\n'
        '</md:EntityDescriptor>'
    )
