"""
Request/response Pydantic models for the EOS WebUI API.

All POST body types are declared here to give FastAPI typed validation,
automatic 422 error responses, and OpenAPI schema generation for every
endpoint.  Import from this module — do not declare inline body dicts.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    """Canonical chat request.

    Accepts either ``text`` (sent by the WebSocket/HTTP frontend) or the legacy
    ``message`` field so that both the JS frontend and direct API callers work.
    Exactly one of the two must be non-empty.
    """

    text: Optional[str] = Field(None, min_length=1, max_length=32_000,
                                description="User message text (preferred field name).")
    message: Optional[str] = Field(None, min_length=1, max_length=32_000,
                                   description="User message text (legacy alias for text).")
    text_attachment: Optional[Dict[str, Any]] = Field(None, description="Text-file attachment metadata (file_id + filename).")

    @model_validator(mode="after")
    def require_text_or_message(self) -> "ChatRequest":
        if not (self.text or self.message):
            raise ValueError("Either 'text' or 'message' must be provided and non-empty.")
        return self

    @property
    def user_message(self) -> str:
        """Return the canonical message string."""
        return (self.text or self.message or "").strip()


# ── Upload ────────────────────────────────────────────────────────────────────

class UploadRequest(BaseModel):
    """JSON-based file upload.

    The frontend encodes file content as base64 and posts JSON (not multipart).
    """
    filename: str = Field(..., min_length=1, max_length=512,
                          description="Original file name.")
    content_type: str = Field("application/octet-stream",
                              description="MIME type of the file.")
    data: str = Field(..., min_length=1,
                      description="Base64-encoded file content.")


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=8_000,
                      description="Text to synthesise.")


# ── Vision ────────────────────────────────────────────────────────────────────

class VisionSettingsRequest(BaseModel):
    enabled: bool = Field(..., description="Enable or disable vision for this session.")


# ── Autonomy ──────────────────────────────────────────────────────────────────

class AutonomyRequest(BaseModel):
    dimension: str = Field(..., min_length=1,
                           description="Autonomy dimension name (e.g. 'perception', 'action').")
    enabled: bool = Field(..., description="Enable or disable the dimension.")


# ── Capabilities ──────────────────────────────────────────────────────────────

class CapabilityRequest(BaseModel):
    group: Literal["autonomy", "computer_use", "workspace", "creativity", "google"] = Field(
        ..., description="Capability group."
    )
    key: str = Field(..., min_length=1, description="Setting key within the group.")
    value: Any = Field(..., description="New value for the setting.")


# ── Computer Use ──────────────────────────────────────────────────────────────

class ComputerUseModeRequest(BaseModel):
    mode: str = Field(..., min_length=1,
                      description="Computer-use mode: 'off', 'command_only', or 'supervised_session'.")
    reason: str = Field("admin panel", description="Human-readable reason for the mode change.")


class ComputerUseHaltRequest(BaseModel):
    reason: str = Field("admin halt", description="Reason for the emergency halt.")


# ── Initiative ────────────────────────────────────────────────────────────────

class InitiativeTriggerRequest(BaseModel):
    rationale: str = Field("manual admin trigger",
                           description="Rationale attached to the evaluation cycle.")


class InitiativeFeedbackRequest(BaseModel):
    initiative_id: str = Field(..., min_length=1, description="ID of the queued initiative.")
    feedback: Literal["accept", "defer", "dismiss"] = Field(
        ..., description="Feedback action: accept, defer, or dismiss."
    )


# ── Investigation ─────────────────────────────────────────────────────────────

class InvestigationCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500,
                       description="Short title for the investigation.")
    description: str = Field("", description="Optional longer description.")
    category: str = Field("general", description="Category tag.")
    priority: int = Field(3, ge=1, le=5, description="Priority 1 (highest) to 5 (lowest).")


class InvestigationRunPassRequest(BaseModel):
    task_type: str = Field("evidence_review",
                           description="Pass task type (e.g. 'evidence_review', 'hypothesis').")
    objective: str = Field("", description="Optional objective override for this pass.")


class InvestigationResolveRequest(BaseModel):
    resolution_summary: str = Field(..., min_length=1,
                                    description="Summary of how the investigation was resolved.")


# ── Secrets ───────────────────────────────────────────────────────────────────

class SecretSetRequest(BaseModel):
    value: str = Field(..., min_length=1, description="Secret value to store in the keyring.")


# ── Diagnostic ────────────────────────────────────────────────────────────────

class ForceToolRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, description="Tool name to force-execute.")
    params: dict = Field(default_factory=dict, description="Parameters for the tool.")


class ForceRetrievalRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Memory retrieval query.")
    n: int = Field(5, ge=1, le=50, description="Number of results to return.")


# ── Goals ─────────────────────────────────────────────────────────────────────

class GoalCreateRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    priority: str = Field("normal", description="Priority: 'high', 'normal', or 'low'.")
    context: str = Field("", description="Optional context for the goal.")
    source: str = Field("admin", description="Who created the goal.")


class GoalNoteRequest(BaseModel):
    note: str = Field("", description="Optional note attached to the status change.")


class GoalAbandonRequest(BaseModel):
    reason: str = Field("", description="Optional reason for abandoning the goal.")


# ── Access Tiers ──────────────────────────────────────────────────────────────

class AccessTierUpdateRequest(BaseModel):
    """Partial update to a tier's policy.  Only provided fields are changed."""
    enabled: Optional[bool]         = Field(None, description="Enable or disable this tier entirely.")
    chat_enabled: Optional[bool]    = Field(None, description="Allow /api/chat from this tier.")
    admin_enabled: Optional[bool]   = Field(None, description="Allow /admin/* from this tier.")
    require_auth: Optional[bool]    = Field(None, description="Require a LAN session token for non-admin routes.")
    rate_limit_rpm: Optional[int]   = Field(None, ge=0, description="Requests per minute (0 = unlimited).")
    rate_limit_burst: Optional[int] = Field(None, ge=0, description="Burst tolerance above steady rate.")
    session_ttl_sec: Optional[int]  = Field(None, ge=60, description="Session token lifetime in seconds.")


class LanPairRequest(BaseModel):
    """Exchange a one-time pairing code for a LAN session token."""
    code: str = Field(..., min_length=1, description="One-time pairing code from the admin panel.")
    label: str = Field("", max_length=100, description="Optional human-readable label for this device.")


class LanSessionRevokeRequest(BaseModel):
    """Revoke a specific LAN session token."""
    token: str = Field(..., min_length=1, description="Session token to revoke.")


# ── Overnight cycle ───────────────────────────────────────────────────────────

class OvernightReturnTimeRequest(BaseModel):
    expected_return_time: str = Field(..., min_length=1, description="ISO-8601 expected return timestamp.")


class OvernightCancelRequest(BaseModel):
    reason: str = Field("user_request", description="Reason for cancelling the active overnight cycle.")
