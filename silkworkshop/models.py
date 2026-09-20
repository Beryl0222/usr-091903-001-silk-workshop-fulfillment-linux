"""履约领域的数据结构。

裁片（MaterialPiece）从面料批次拆分后形成树：parent_id 指回被拆分的裁片，
batch_id 一路继承，因此拆分、退回工坊、改作展示品都不会丢失来源。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# 裁片状态：在库 / 已预留 / 已领用 / 已入作品 / 退回工坊 / 展示品 / 已拆分（母片终结）
PIECE_STATUSES = ("stock", "reserved", "issued", "consumed", "returned", "display", "split")
# 场次状态：已排期 / 已改期 / 已取消 / 已完成
SESSION_STATUSES = ("scheduled", "rescheduled", "cancelled", "completed")
# 授权类别：未成年人影像 / 顾客故事 / 作品照片
CONSENT_CATEGORIES = ("minor_imagery", "customer_story", "work_photo")


class DictMixin:
    def to_dict(self):
        return asdict(self)


@dataclass
class FabricBatch(DictMixin):
    """面料批次：裁衣剩余真丝的入库记录。"""

    id: str
    name: str
    source_order: str  # 来源裁衣单号
    designer_id: str  # 设计师（核清边角料再利用量）
    silk_type: str
    total_area_cm2: float
    dye_notes: str = ""  # 染色注意事项
    cleaning_notes: str = ""  # 清洁注意事项
    reuse_value_per_cm2_pence: int = 0  # 边角料再利用估值（便士/cm²）
    created_at: str = ""


@dataclass
class MaterialPiece(DictMixin):
    """裁片：批次的一部分，可被继续拆分。"""

    id: str
    batch_id: str
    parent_id: Optional[str]
    label: str
    area_cm2: float
    status: str = "stock"
    location: Optional[str] = None  # 所在场次 id
    holder: Optional[str] = None  # 当前经手人
    work_id: Optional[str] = None  # 消耗入哪个作品
    created_at: str = ""


@dataclass
class Movement(DictMixin):
    """流转台账：追加式，永不修改，是经手人链与来源链的依据。"""

    id: str
    seq: int
    piece_id: str
    kind: str  # register/split/split_out/reserve/release/issue/consume/return_workshop/display/handover
    from_status: Optional[str]
    to_status: Optional[str]
    area_cm2: float
    actor: str
    session_id: Optional[str] = None
    work_id: Optional[str] = None
    note: str = ""
    created_at: str = ""


@dataclass
class ActivityPlan(DictMixin):
    """活动方案：工艺、用料标准、讲解内容与费用约定。"""

    id: str
    title: str
    craft_type: str  # 手链 / 发簪 / 耳饰
    material_per_participant_cm2: float
    briefing: str  # 工艺讲解内容
    briefing_version: int
    required_qualifications: list
    fee_pence: int
    instructor_share_percent: int  # 讲师报酬占报名费比例
    created_at: str = ""


@dataclass
class Instructor(DictMixin):
    id: str
    name: str
    qualifications: list
    active: bool = True
    created_at: str = ""


@dataclass
class Session(DictMixin):
    """场次：方案的一次排期。commitments 保存原承诺快照，改期不动它。"""

    id: str
    plan_id: str
    instructor_id: str
    starts_at: str
    capacity: int
    status: str = "scheduled"
    commitments: dict = field(default_factory=dict)
    reservations: list = field(default_factory=list)  # 历史上预留过的裁片 id
    history: list = field(default_factory=list)
    takeover: Optional[dict] = None  # 取消时：材料/讲解/授权由谁接手
    last_sufficiency: Optional[dict] = None
    created_at: str = ""


@dataclass
class Participant(DictMixin):
    id: str
    name: str
    is_minor: bool = False
    guardian_name: str = ""
    guardian_contact: str = ""
    created_at: str = ""


@dataclass
class Enrollment(DictMixin):
    """报名：参与者与场次的关联，choices 记录参与者选择。"""

    id: str
    session_id: str
    participant_id: str
    choices: dict = field(default_factory=dict)
    fee_pence: int = 0
    status: str = "active"
    created_at: str = ""


@dataclass
class Work(DictMixin):
    """体验作品：登记时一次性扣减用料。"""

    id: str
    session_id: str
    participant_id: str
    craft_type: str
    title: str
    piece_usages: list  # [{piece_id, area_cm2, batch_id}]
    offline_ref: Optional[str] = None  # 断网补录的客户端凭据
    created_by: str = ""
    created_at: str = ""


@dataclass
class Consent(DictMixin):
    """授权：按主体、类别、用途、期限分别取得。"""

    id: str
    subject_type: str  # participant / work
    subject_id: str
    category: str  # CONSENT_CATEGORIES
    purpose: str  # 用途，如 in_store_display / social_media
    granted_by: str
    granted_by_role: str  # self / guardian
    granted_at: str
    expires_at: str  # 期限
    status: str = "active"  # active / withdrawn
    withdrawn_at: Optional[str] = None
    note: str = ""


@dataclass
class Publication(DictMixin):
    """一次展示使用：必须落在有效授权内。"""

    id: str
    consent_id: str
    subject_type: str
    subject_id: str
    category: str
    purpose: str
    channel: str
    ref: str = ""
    status: str = "active"  # active / review_required（授权撤回后列入复核）
    created_at: str = ""


@dataclass
class Handover(DictMixin):
    """换老师交接单：材料、讲解版本、待跟进授权。"""

    id: str
    session_id: str
    from_instructor_id: str
    to_instructor_id: str
    materials: list
    briefing_version: int
    consent_ids: list
    reason: str
    actor: str
    created_at: str = ""


@dataclass
class FeeEntry(DictMixin):
    """费用台账：报名费、讲师报酬、材料再利用估值、门店留存、讲师付款。"""

    id: str
    session_id: str
    kind: str  # participant_fee / instructor_payable / material_reuse / ops_remainder / instructor_payout
    amount_pence: int
    enrollment_id: Optional[str] = None
    instructor_id: Optional[str] = None
    note: str = ""
    created_at: str = ""
