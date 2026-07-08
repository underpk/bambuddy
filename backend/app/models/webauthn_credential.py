"""WebAuthn (passkey) credentials.

Each row is one passkey registered by a user — a platform authenticator
(fingerprint/face on phone, Windows Hello) or a roaming key (YubiKey).
The private key never leaves the authenticator; we store only the public
key and the signature counter used for clone detection.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class WebAuthnCredential(Base):
    __tablename__ = "webauthn_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Credential ID as base64url — what the authenticator sends on login
    credential_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)

    # COSE public key as base64url
    public_key: Mapped[str] = mapped_column(Text, nullable=False)

    # Signature counter — monotonically increasing; a decrease indicates a cloned key
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # JSON-encoded list of transports reported at registration (e.g. ["internal","hybrid"])
    transports: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # User-facing label, e.g. "Galaxy S26 Ultra"
    device_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
