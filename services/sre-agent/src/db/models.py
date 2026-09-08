import datetime
import uuid
from typing import Optional
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, JSON

class Base(DeclarativeBase):
    pass

class AgentLog(Base):                                                                                                                                                                                                                      
    __tablename__ = "agent_logs"                                                                                                                                                                                                           
                                                                                                                                                                                                                                               
    id: Mapped[int] = mapped_column(primary_key=True)                                                                                                                                                                                      
    timestamp: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)                                                                                                                                                 
    container_name: Mapped[str] = mapped_column(String(100))                                                                                                                                                                               
    log_level: Mapped[str] = mapped_column(String(20))                                                                                                                                                                                     
    message: Mapped[str] = mapped_column(Text)                                                                                                                                                                                             
    status_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    

class SystemHealth(Base):                                                                                                                                                                                                                  
    __tablename__ = "system_health"                                                                                                                                                                                                        

    id: Mapped[int] = mapped_column(primary_key=True)                                                                                                                                                                                      
    timestamp: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)                                                                                                                                                 
    cpu_percent: Mapped[Optional[float]] = mapped_column(nullable=True)                                                                                                                                                                    
    ram_usage_mb: Mapped[Optional[float]] = mapped_column(nullable=True)                                                                                                                                                                   
    ram_total_mb: Mapped[Optional[float]] = mapped_column(nullable=True)                                                                                                                                                                   
    disk_usage_gb: Mapped[Optional[float]] = mapped_column(nullable=True)                                                                                                                                                                  
    disk_total_gb: Mapped[Optional[float]] = mapped_column(nullable=True)                                                                                                                                                                  
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                                                                                                                                                                      

class AuditTrail(Base):                                                                                                                                                                                                                    
    __tablename__ = "audit_trails"                                                                                                                                                                                                         

    id: Mapped[int] = mapped_column(primary_key=True)                                                                                                                                                                                      
    timestamp: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)                                                                                                                                                 
    action_type: Mapped[str] = mapped_column(String(50))  # e.g., 'restart', 'health_check', 'vcs_pr'                                                                                                                                      
    target: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)                                                                                                                                                              
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING, APPROVED, REJECTED, SUCCESS, FAILED                                                                                                                     
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                                                                                                                                                                    


class ActionApproval(Base):
    """Durable exact-action approval. Jira workflow state is not authorization."""

    __tablename__ = "action_approvals"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    action_type: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict] = mapped_column(JSON)
    arguments_hash: Mapped[str] = mapped_column(String(64), index=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), unique=True)
    risk_class: Mapped[str] = mapped_column(String(4))
    policy_version: Mapped[str] = mapped_column(String(50))
    requested_by: Mapped[str] = mapped_column(String(200))
    approved_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    decided_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    consumed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
