from enum import StrEnum


class Role(StrEnum):
    tenant_admin = "tenant_admin"
    analyst = "analyst"
    contributor = "contributor"


class ConsentType(StrEnum):
    geolocation = "geolocation"
    microphone = "microphone"
    survey_data = "survey_data"
    photo = "photo"
    study_data = "study_data"

class CalibrationStatus(StrEnum):
    calibrated = "calibrated"
    uncalibrated = "uncalibrated"


class QualityStatus(StrEnum):
    pending = "pending"
    validated = "validated"
    indicative = "indicative"
    rejected = "rejected"


class DayPart(StrEnum):
    morning = "morning"
    afternoon = "afternoon"
    evening = "evening"
    night = "night"


class ProposalStatus(StrEnum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"
    completed = "completed"


class TriggerMode(StrEnum):
    scheduled = "scheduled"
    contextual = "contextual"


class ScheduleMode(StrEnum):
    scheduled = "scheduled"
    contextual = "contextual"
    both = "both"


class NoiseContext(StrEnum):
    traffic = "traffic"
    commerce = "commerce"
    construction = "construction"
    music = "music"
    gathering = "gathering"
    other = "other"


class ReportKind(StrEnum):
    csv = "csv"
    pdf = "pdf"


class ReportStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"




class InsightKind(StrEnum):
    cell_analysis = "cell_analysis"
    anomaly_digest = "anomaly_digest"
    report_summary = "report_summary"
    data_analysis = "data_analysis"


class MissionStatus(StrEnum):
    offered = "offered"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"
    completed = "completed"
    failed = "failed"



class SurveyDomain(StrEnum):
    demographie = "demographie"
    menages = "menages"
    habitat = "habitat"
    emploi = "emploi"
    entreprises = "entreprises"
    agriculture = "agriculture"
    education = "education"
    sante = "sante"
    economie = "economie"
    migration = "migration"
    genre = "genre"
    numerique = "numerique"


class SurveyStatus(StrEnum):
    draft = "draft"
    active = "active"
    paused = "paused"
    closed = "closed"


class ProfileStatus(StrEnum):
    active = "active"
    retired = "retired"


class EvidenceStatus(StrEnum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class StudyStatus(StrEnum):
    draft = "draft"
    active = "active"
    paused = "paused"
    closed = "closed"


class StudyKind(StrEnum):
    survey = "survey"
    poll = "poll"
    market_study = "market_study"
    field_study = "field_study"
    commercial = "commercial"
    product_test = "product_test"


class SponsorKind(StrEnum):
    company = "company"
    ngo = "ngo"
    person = "person"
    service = "service"
    institution = "institution"


class AssetKind(StrEnum):
    image = "image"
    video = "video"


class ParticipationStatus(StrEnum):
    invited = "invited"
    seen = "seen"
    started = "started"
    completed = "completed"
    screened_out = "screened_out"
    declined = "declined"
    expired = "expired"


class RewardStatus(StrEnum):
    pending = "pending"
    entered = "entered"
    granted = "granted"
    delivered = "delivered"
    refused = "refused"
    revoked = "revoked"
    expired = "expired"

