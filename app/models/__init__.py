from app.models.collection import (CollectionProposal, CollectionSchedule, TenantAlgorithmState,  # noqa: F401
                                   UserResponseStat)
from app.models.devices import Device, DeviceCalibration, DeviceModel  # noqa: F401
from app.models.geo import GeographicCell, NoiseAggregation  # noqa: F401
from app.models.insights import AIInsight, Report  # noqa: F401
from app.models.measurement import Measurement, MeasurementQuality  # noqa: F401
from app.models.quota import Mission, QuotaDay, UserQuota  # noqa: F401
from app.models.roads import RoadImportTile, RoadSegment  # noqa: F401
from app.models.tenancy import AuditLog, Tenant, User, UserConsent  # noqa: F401