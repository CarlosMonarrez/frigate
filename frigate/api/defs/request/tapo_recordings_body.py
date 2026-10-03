"""Request bodies for downloading recordings from Tapo SD cards."""

from datetime import date

from pydantic import BaseModel, Field, SecretStr


class TapoCredentials(BaseModel):
    """Credentials used only for a single Tapo camera request."""

    username: str = Field(default="admin", min_length=1, max_length=128)
    camera_password: SecretStr
    cloud_password: SecretStr


class TapoRecordingsRequest(TapoCredentials):
    """Request to list recordings from selected cameras for one day."""

    camera_names: list[str] = Field(min_length=1, max_length=8)
    date: date


class TapoRecordingSelection(BaseModel):
    """One SD-card recording selected for download."""

    camera_name: str = Field(min_length=1, max_length=20)
    start_time: int = Field(gt=0)
    end_time: int = Field(gt=0)


class TapoDownloadRequest(TapoCredentials):
    """Request to download selected SD-card recordings as a ZIP archive."""

    date: date
    recordings: list[TapoRecordingSelection] = Field(min_length=1, max_length=5000)
