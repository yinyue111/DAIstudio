"""账号自助注销（两阶段软注销,个人信息保护法合规通道）。

第一阶段（本模块,立即生效,单事务完成）:
- 前置校验:有冻结积分、进行中的生成/反推任务、未支付且未过期的充值订单时阻止注销;
- 剩余可用积分不自动退款,必须由用户显式确认放弃,放弃动作写入 credit_transactions
  （type=consume, biz_type=account_deletion）保证台账闭环;
- 个人可识别信息立即脱敏:手机号替换为不可逆哈希占位（保留末四位便于客服核对）、
  昵称/头像/部门/密码清空,状态置为 disabled,token_version 自增吊销全部已发放登录态;
- 用户草稿（user_drafts,含个人工作台内容）立即删除。

第二阶段（依赖既有机制,本模块不重复实现）:
- 素材/上传/解析记录按 services/retention.py 既有保留策略到期自动清除;
- 财务与审计记录（credit_transactions / payment_orders / gen_tasks / audit_logs）
  按既有法定期限留存;user 行脱敏后这些记录仅剩 user_id 数字外键,
  不再能关联到自然人,即"保留账单、解绑个人"。

注销不可撤销（无冷静期）:立即释放手机号供重新注册与"可撤销"互斥,
且平台登录/鉴权链路统一拒绝非 active 账号,无法为待注销账号开撤销通道。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenTask, PaymentOrder, ReverseOperation, User, UserDraft
from . import audit, credits

# 阻止注销的活跃任务状态:queued/running 尚未结算,needs_review 冻结积分未回收。
_ACTIVE_GEN_TASK_STATUSES = ("queued", "running", "needs_review")
_ACTIVE_REVERSE_STATUSES = ("queued", "running", "needs_confirmation")

_ANON_PHONE_PREFIX = "del"


class AccountDeletionBlocked(Exception):
    """有未了结事项,当前不能注销。blockers 为 [{code, message}]。"""

    def __init__(self, blockers: list[dict]):
        self.blockers = blockers
        super().__init__("; ".join(b["message"] for b in blockers))


class CreditsForfeitConfirmationRequired(Exception):
    """剩余可用积分需要用户显式确认放弃（不自动退款）。"""

    def __init__(self, balance_credits: int):
        self.balance_credits = int(balance_credits)
        super().__init__(
            f"账户仍有 {self.balance_credits} 可用积分,注销后将作废且不退款,需显式确认"
        )


def mask_phone(phone: str) -> str:
    """展示用掩码:保留前三位与末四位。"""
    if len(phone) >= 7:
        return f"{phone[:3]}****{phone[-4:]}"
    return "****"


def anonymized_phone(phone: str) -> str:
    """不可逆脱敏占位,写回 users.phone（唯一列,String(20)）。

    形如 del{末四位}{12位哈希}（共 19 字符）:
    - 前缀 + 末四位便于客服凭部分号码核对历史工单;
    - 哈希片段以服务端密钥加盐,保证唯一性且不可由占位反推原号;
    - 原手机号从 users 表消失,即刻可被重新注册。
    """
    digest = hashlib.sha256(
        f"{settings.jwt_secret}:account-deletion:{phone}".encode()
    ).hexdigest()[:12]
    return f"{_ANON_PHONE_PREFIX}{phone[-4:]}{digest}"


def is_anonymized_phone(value: str) -> bool:
    return value.startswith(_ANON_PHONE_PREFIX) and len(value) == 19


def _count(db: Session, stmt) -> int:
    return int(db.execute(stmt).scalar() or 0)


def list_blockers(db: Session, user: User) -> list[dict]:
    """注销前置校验:返回全部阻断项（空列表表示可注销）。"""
    blockers: list[dict] = []
    frozen = int(user.frozen_credits or 0)
    if frozen > 0:
        blockers.append({
            "code": "frozen_credits",
            "message": (
                f"账户有 {frozen} 冻结积分未结算,请等待相关任务完成或失败退回后再注销"
            ),
        })
    active_tasks = _count(
        db,
        select(func.count(GenTask.id)).where(
            GenTask.user_id == user.id,
            GenTask.status.in_(_ACTIVE_GEN_TASK_STATUSES),
        ),
    )
    if active_tasks:
        blockers.append({
            "code": "active_generation_tasks",
            "message": f"有 {active_tasks} 个未完成的生成任务,请等待任务结束后再注销",
        })
    active_reverse = _count(
        db,
        select(func.count(ReverseOperation.id)).where(
            ReverseOperation.user_id == user.id,
            ReverseOperation.status.in_(_ACTIVE_REVERSE_STATUSES),
        ),
    )
    if active_reverse:
        blockers.append({
            "code": "active_reverse_operations",
            "message": f"有 {active_reverse} 个未完成的反推任务,请等待任务结束后再注销",
        })
    now = datetime.now(timezone.utc)
    pending_orders = _count(
        db,
        select(func.count(PaymentOrder.id)).where(
            PaymentOrder.user_id == user.id,
            PaymentOrder.status == "pending",
            or_(PaymentOrder.expires_at.is_(None), PaymentOrder.expires_at > now),
        ),
    )
    if pending_orders:
        blockers.append({
            "code": "pending_payment_orders",
            "message": (
                f"有 {pending_orders} 笔待支付的充值订单,请等待订单支付完成或关闭后再注销"
            ),
        })
    return blockers


def preflight(db: Session, user: User) -> dict:
    """注销预检:给前端展示阻断项与需要放弃的积分。"""
    if user.deleted_at is not None:
        return {
            "can_delete": False,
            "already_deleted": True,
            "requires_credit_forfeit_confirmation": False,
            "balance_credits": 0,
            "frozen_credits": 0,
            "blockers": [{"code": "already_deleted", "message": "账号已注销"}],
        }
    blockers = list_blockers(db, user)
    balance = int(user.balance_credits or 0)
    return {
        "can_delete": not blockers,
        "already_deleted": False,
        "requires_credit_forfeit_confirmation": balance > 0,
        "balance_credits": balance,
        "frozen_credits": int(user.frozen_credits or 0),
        "blockers": blockers,
    }


def delete_account(
    db: Session,
    user_id: int,
    *,
    confirm_forfeit_credits: bool = False,
    ip: str | None = None,
) -> dict:
    """执行软注销。幂等:已注销账号重复调用直接返回成功。

    调用方（路由层）负责身份与密码校验;本函数在行锁下复核前置条件,
    避免并发任务提交/重复注销竞态。成功后由本函数提交事务。
    """
    # 行锁串行化同一账号的并发注销/扣费。
    user = db.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one()
    if user.deleted_at is not None:
        db.rollback()
        return {
            "ok": True,
            "already_deleted": True,
            "deleted_at": user.deleted_at,
            "phone_masked": "****",
            "forfeited_credits": 0,
        }

    blockers = list_blockers(db, user)
    if blockers:
        db.rollback()
        raise AccountDeletionBlocked(blockers)

    balance = int(user.balance_credits or 0)
    if balance > 0 and not confirm_forfeit_credits:
        db.rollback()
        raise CreditsForfeitConfirmationRequired(balance)

    original_phone = user.phone
    phone_masked = mask_phone(original_phone)
    forfeited = 0
    if balance > 0:
        # 显式放弃剩余积分:写台账（consume,balance_after=0）而不是直接清零,
        # 保证 credit_transactions 与 users.balance_credits 始终对得上。
        credits.consume(
            db,
            user.id,
            balance,
            biz_type="account_deletion",
            biz_ref=user.id,
            note=f"账号注销,用户确认放弃剩余积分 {balance}",
            commit=False,
        )
        forfeited = balance

    now = datetime.now(timezone.utc)
    user.phone = anonymized_phone(original_phone)
    user.password_hash = None
    user.nickname = None
    user.avatar = None
    user.department = None
    user.status = "disabled"
    user.deleted_at = now
    user.token_version += 1  # 吊销全部已发放 token

    # 用户草稿含个人工作台内容,属个人信息,立即删除。
    db.execute(delete(UserDraft).where(UserDraft.user_id == user.id))

    # 审计随业务事务一起提交;detail 只落掩码手机号,不落原号。
    audit.log_required(
        db,
        user_id=user.id,
        action="account_deletion",
        biz_type="account",
        biz_id=user.id,
        ip=ip,
        detail={"phone_masked": phone_masked, "forfeited_credits": forfeited},
    )
    db.commit()
    return {
        "ok": True,
        "already_deleted": False,
        "deleted_at": now,
        "phone_masked": phone_masked,
        "forfeited_credits": forfeited,
    }
