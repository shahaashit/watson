from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class CaptureIn(BaseModel):
    text: str = Field(min_length=1)
    work_item_id: Optional[int] = None


class SnoozeIn(BaseModel):
    until: str  # ISO datetime


class ActionPatch(BaseModel):
    payload: Optional[dict] = None
    target_id: Optional[str] = None


class AskIn(BaseModel):
    question: str = Field(min_length=1)


class BulkActionIn(BaseModel):
    op: Literal["approve", "reject"]
    ids: List[int] = Field(min_length=1)


WorkItemState = Literal["today", "next", "waiting", "done"]


class WorkItemCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    state: WorkItemState = "next"
    owner_person_id: Optional[int] = None
    owner_display: str = ""


class WorkItemMove(BaseModel):
    state: WorkItemState
    before_id: Optional[int] = None
    after_id: Optional[int] = None


class WorkItemPatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    owner_person_id: Optional[int] = None
    owner_display: Optional[str] = None


class WorkActivityCreate(BaseModel):
    activity_type: Literal["note", "decision", "blocker"]
    body: str = Field(min_length=1)


class WorkLinkCreate(BaseModel):
    source_type: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    url: str = ""
    label: str = ""


class WorkImportIn(BaseModel):
    url: str = Field(min_length=1)

    @field_validator("url", mode="before")
    @classmethod
    def strip_url(cls, value):
        return value.strip() if isinstance(value, str) else value


# --- Settings and onboarding -------------------------------------------


class StrictSettingsModel(BaseModel):
    """Settings payloads reject unknown keys instead of becoming a KV API."""

    model_config = ConfigDict(extra="forbid")


class ProfileSettingsIn(StrictSettingsModel):
    display_name: Optional[str] = Field(default=None, min_length=1)
    timezone: Optional[str] = Field(default=None, min_length=1)
    email_domain: Optional[str] = Field(default=None, min_length=1)
    separate_work_by_status: Optional[bool] = None
    auto_create_review_tasks: Optional[bool] = None
    ai_group_review_mrs: Optional[bool] = None

    @field_validator("display_name", "timezone", "email_domain", mode="before")
    @classmethod
    def strip_profile_values(cls, value):
        return value.strip() if isinstance(value, str) else value


class GitLabSettingsIn(StrictSettingsModel):
    base_url: str
    token: Optional[SecretStr] = None
    username: str = ""


class GitLabProjectsIn(StrictSettingsModel):
    project_ids: List[int] = Field(default_factory=list)

    @field_validator("project_ids")
    @classmethod
    def validate_project_ids(cls, values):
        if any(value <= 0 for value in values):
            raise ValueError("project IDs must be positive")
        if len(values) != len(set(values)):
            raise ValueError("project IDs must be unique")
        return values


class ClickUpSettingsIn(StrictSettingsModel):
    token: Optional[SecretStr] = None
    create_list_id: str = ""


class AnthropicSettingsIn(StrictSettingsModel):
    api_key: Optional[SecretStr] = None
    base_url: str = ""
    model: str = "claude-sonnet-4-6"


class DataSettingsIn(StrictSettingsModel):
    backup_dir: str = Field(min_length=1)

    @field_validator("backup_dir", mode="before")
    @classmethod
    def strip_backup_dir(cls, value):
        return value.strip() if isinstance(value, str) else value


class GoogleCalendarSettingsIn(StrictSettingsModel):
    client_config_json: Optional[SecretStr] = None


class FlockSettingsIn(StrictSettingsModel):
    profile_dir: str = ""
    user_handle: str = ""


IdentitySource = Literal["gitlab", "clickup", "flock", "google-calendar"]


class PersonIdentityIn(StrictSettingsModel):
    source: IdentitySource
    external_id: str = Field(min_length=1)
    display_value: str = ""

    @field_validator("source", "external_id", mode="before")
    @classmethod
    def strip_identity_values(cls, value):
        return value.strip() if isinstance(value, str) else value


class PersonIn(StrictSettingsModel):
    display_name: str = Field(min_length=1)
    identifier: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    @field_validator("display_name", "identifier", mode="before")
    @classmethod
    def normalize_person_values(cls, value, info):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value.casefold() if info.field_name == "identifier" else value


class PersonPatch(StrictSettingsModel):
    display_name: Optional[str] = Field(default=None, min_length=1)
    identifier: Optional[str] = Field(
        default=None, min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )

    @field_validator("display_name", "identifier", mode="before")
    @classmethod
    def normalize_person_values(cls, value, info):
        if not isinstance(value, str):
            return value
        value = value.strip()
        return value.casefold() if info.field_name == "identifier" else value


class OnboardingPatch(StrictSettingsModel):
    completed: Optional[bool] = None
    step: Optional[int] = Field(default=None, ge=1, le=4)


class NoteIn(BaseModel):
    body: str = Field(min_length=1)


class WorkInboxResolve(BaseModel):
    work_item_id: int


# --- LLM classification output -----------------------------------------

EntryType = Literal["discussion", "meeting", "task", "note", "status_update"]


class ClassifiedEntry(BaseModel):
    type: EntryType = "note"
    title: str
    body: str = ""
    people: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    # id of an existing entry this capture follows up on (threads under it), or null
    follows_up_entry_id: Optional[int] = None


class ClassifiedReminder(BaseModel):
    text: str
    due_at: str


class ClickUpActionDraft(BaseModel):
    kind: Literal["clickup_comment", "clickup_status", "clickup_create_task"]
    task_match_query: str = ""
    suggested_task_id: Optional[str] = None
    suggested_mr_id: Optional[str] = None  # GitLab MR (format "<project_id>!<iid>")
    # If the capture clearly belongs to an existing Watson card the LLM saw
    # in the managed_tasks list, set this to the matching card's *related*
    # ClickUp task id. The classifier will then either:
    #   * draft `link_mr_to_task` (when a valid suggested_mr_id is also set), OR
    #   * suppress the create-task action entirely (the capture is logged as
    #     entries only — no duplicate ClickUp task is made).
    existing_work_clickup_id: Optional[str] = None
    draft: Any = ""


class GcalEventDraft(BaseModel):
    """LLM-drafted Google Calendar event that Watson will create if the user
    approves. Only surfaces when the capture explicitly asks Watson to
    schedule / book / send a calendar invite — never auto-drafted from a
    plain reminder or discussion note.
    """
    title: str
    start_at: str            # ISO 8601, LLM should resolve relative phrases
    end_at: Optional[str] = None
    duration_minutes: int = 30   # used only if end_at is missing
    attendees: List[str] = Field(default_factory=list)  # email addresses
    description: str = ""
    add_meet: bool = True   # attach a Google Meet link on the event


class Classification(BaseModel):
    entries: List[ClassifiedEntry] = Field(default_factory=list)
    reminders: List[ClassifiedReminder] = Field(default_factory=list)
    clickup_actions: List[ClickUpActionDraft] = Field(default_factory=list)
    gcal_events: List[GcalEventDraft] = Field(default_factory=list)
