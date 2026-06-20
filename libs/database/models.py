from sqlalchemy.orm import DeclarativeBase
import sqlalchemy as sa
from typing import Optional
from sqlalchemy import String, ForeignKey, Column, Integer, Text, DateTime, JSON, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship
import datetime
import sqlalchemy_utils
from sqlalchemy_utils import create_database, database_exists

def validate_database(url):
    if not sqlalchemy_utils.database_exists(url):
        sqlalchemy_utils.create_database(url)
        
class Base(DeclarativeBase):
    pass

def validate_database(url):
    """Method for validating database url exists or not

    :raises: None
    :returns: None
    """
    if not database_exists(url):
        create_database(url)
        
        
class User(Base):
    __tablename__ = "platform_users"
    
    id: Mapped[str] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(120))
    
class AgentLog(Base):
    __tablename__ = "agent_logs"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)
    container_name: Mapped[str] = mapped_column(String(100))
    log_level: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    status_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

class ComplianceStatus(Enum):
    HALAL = "HALAL"
    DOUBTFUL = "DOUBTFUL"
    HARAM = "HARAM"

class ComplianceScan(Base):
    __tablename__ = "compliance_scans"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    compliance_grade: Mapped[ComplianceStatus] = mapped_column(
        sa.Enum("HALAL", "DOUBTFUL", "HARAM", name="compliance_status_enum")
    )
    purification_amount: Mapped[Optional[float]] = mapped_column()
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    timestamp: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)

class TradeProposal(Base):
    __tablename__ = "trade_proposals"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(10)) # BUY/SELL
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    user_id: Mapped[str] = mapped_column(ForeignKey("platform_users.id"))
    
    # Relationship to User
    user: Mapped["User"] = relationship()
