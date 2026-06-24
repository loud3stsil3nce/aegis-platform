import datetime
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

class KnowledgeIngestionRun(Base):
    __tablename__ = "knowledge_ingestion_runs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(50))  # "aaoifi_pdf" | "codebase"
    source_path: Mapped[str] = mapped_column(Text)
    chunks_upserted: Mapped[int] = mapped_column()
    run_at: Mapped[datetime.datetime] = mapped_column(default=datetime.datetime.utcnow)
    status: Mapped[str] = mapped_column(String(20))  # "success" | "failed"
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)