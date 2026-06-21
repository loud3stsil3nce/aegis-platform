import os
import json
from github import Github
from fastmcp import FastMCP
from src.db.database import async_session
from src.db.models import AuditTrail
from sqlalchemy import select

GITHUB_PAT = os.getenv("GITHUB_PAT")

def get_github_client():                                                                                      
    if not GITHUB_PAT:                                                                                        
        raise ValueError("Configuration Error: GITHUB_PAT environment variable is not set.")                  
    return Github(GITHUB_PAT)  

def register_vcs_tools(mcp: FastMCP):                                                                                                                                                     
                                                                                                                                                                                              
    @mcp.tool()                                                                                                                                                                           
    def get_file_from_api(repo_name: str, path: str, ref: str = "main") -> str:                                                                                                           
        """                                                                                                                                                                               
        Fetches the content of a file directly from the GitHub repository via REST API.                                                                                                   
        No local cloning occurs.                                                                                                                                                          
        """                                                                                                                                                                               
        try:                                                                                                                                                                              
            g = get_github_client()                                                                                                                                                       
            repo = g.get_repo(repo_name)                                                                                                                                                  
            contents = repo.get_contents(path, ref=ref)                                                                                                                                   
            return contents.decoded_content.decode("utf-8")                                                                                                                               
        except Exception as e:                                                                                                                                                            
            return f"Failed to retrieve file: {str(e)}"                                                                                                                                   
                                                                                                                                                                                            
    @mcp.tool()                                                                                                                                                                           
    def create_branch(repo_name: str, new_branch: str, base_branch: str = "main") -> str:                                                                                                 
        """                                                                                                                                                                               
        Creates a new branch on GitHub via REST API.                                                                                                                                      
        """                                                                                                                                                                               
        try:                                                                                                                                                                              
            g = get_github_client()                                                                                                                                                       
            repo = g.get_repo(repo_name)                                                                                                                                                  
            base_ref = repo.get_branch(base_branch)                                                                                                                                       
            repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base_ref.commit.sha)                                                                                                  
            return f"✅ Branch '{new_branch}' successfully created from '{base_branch}'."                                                                                                 
        except Exception as e:                                                                                                                                                            
            return f"Failed to create branch: {str(e)}"                                                                                                                                   
                                                                                                                                                                                            
    @mcp.tool()                                                                                                                                                                           
    def commit_file_change(repo_name: str, path: str, new_content: str, commit_message: str, branch: str) -> str:                                                                         
        """                                                                                                                                                                               
        Modifies a file in-memory and commits it directly to a branch via REST API.                                                                                                       
        """                                                                                                                                                                               
        try:                                                                                                                                                                              
            g = get_github_client()                                                                                                                                                       
            repo = g.get_repo(repo_name)                                                                                                                                                  
            contents = repo.get_contents(path, ref=branch)                                                                                                                                
            repo.update_file(                                                                                                                                                             
                path=path,                                                                                                                                                                
                message=commit_message,                                                                                                                                                   
                content=new_content,                                                                                                                                                      
                sha=contents.sha,                                                                                                                                                         
                branch=branch                                                                                                                                                             
            )                                                                                                                                                                             
            return f"✅ File '{path}' successfully modified and committed to branch '{branch}'."                                                                                          
        except Exception as e:                                                                                                                                                            
            return f"Failed to commit file change: {str(e)}"                                                                                                                              
                                                                                                                                                                                            
    @mcp.tool()                                                                                                                                                                           
    async def create_pr(repo_name: str, title: str, body: str, head_branch: str, base_branch: str = "main", approved_action_id: int = None) -> str:                                       
        """                                                                                                                                                                               
        Submits a Pull Request on GitHub.                                                                                                                                                 
        Gated by a Human-in-the-Loop (HITL) approval mechanism.                                                                                                                           
        If called without an approved_action_id, it registers a PENDING action in the audit logs.                                                                                         
        If called with an approved_action_id that is set to APPROVED in db_sre, it executes the PR creation.                                                                              
        """                                                                                                                                                                               
        try:                                                                                                                                                                              
            g = get_github_client()                                                                                                                                                       
                                                                                                                                                                                            
            # Scenario A: Execute if an approved action ID is provided                                                                                                                    
            if approved_action_id is not None:                                                                                                                                            
                async with async_session() as session:                                                                                                                                    
                    stmt = select(AuditTrail).where(AuditTrail.id == approved_action_id)                                                                                                  
                    result = await session.execute(stmt)                                                                                                                                  
                    action = result.scalar_one_or_none()                                                                                                                                  
                                                                                                                                                                                            
                    if not action:                                                                                                                                                        
                        return f"🚨 Error: Action ID {approved_action_id} not found in SRE audit trails."                                                                                 
                                                                                                                                                                                            
                    if action.status != "APPROVED":                                                                                                                                       
                        return f"🚨 Error: Action ID {approved_action_id} status is '{action.status}'. It must be 'APPROVED' by a human first."                                           
                                                                                                                                                                                            
                    # Connection authorized. Proceed to create Pull Request                                                                                                               
                    repo = g.get_repo(repo_name)                                                                                                                                          
                    pr = repo.create_pull(                                                                                                                                                
                        title=title,                                                                                                                                                      
                        body=body,                                                                                                                                                        
                        head=head_branch,                                                                                                                                                 
                        base=base_branch                                                                                                                                                  
                    )                                                                                                                                                                     
                                                                                                                                                                                            
                    # Update audit status to SUCCESS                                                                                                                                      
                    action.status = "SUCCESS"                                                                                                                                             
                    action.details = f"PR created successfully: {pr.html_url}"                                                                                                            
                    await session.commit()                                                                                                                                                
                                                                                                                                                                                            
                    return f"🎉 Success! PR created: {pr.html_url}"                                                                                                                       
                                                                                                                                                                                            
            # Scenario B: Register a new PENDING PR request in SRE audit logs for human approval                                                                                          
            async with async_session() as session:                                                                                                                                        
                async with session.begin():                                                                                                                                               
                    pr_details = {                                                                                                                                                        
                        "repo_name": repo_name,                                                                                                                                           
                        "title": title,                                                                                                                                                   
                        "body": body,                                                                                                                                                     
                        "head_branch": head_branch,                                                                                                                                       
                        "base_branch": base_branch                                                                                                                                        
                    }                                                                                                                                                                     
                    new_action = AuditTrail(                                                                                                                                              
                        action_type="vcs_pr",                                                                                                                                             
                        target=repo_name,                                                                                                                                                 
                        status="PENDING",                                                                                                                                                 
                        details=json.dumps(pr_details)                                                                                                                                    
                    )                                                                                                                                                                     
                    session.add(new_action)                                                                                                                                               
                    # flush to fetch the auto-generated id                                                                                                                                
                    await session.flush()                                                                                                                                                 
                    action_id = new_action.id                                                                                                                                             
                                                                                                                                                                                            
            return (                                                                                                                                                                      
                f"⚠️ HUMAN-IN-THE-LOOP APPROVAL REQUIRED:\n"
                f"Proposed PR: '{title}' from branch '{head_branch}' into '{base_branch}'.\n"
                f"Action ID: {action_id} has been registered as PENDING in the SRE Audit Trail.\n"
                f"👉 To authorize, update this record to 'APPROVED' in the database (or via dashboard) and execute create_pr with approved_action_id={action_id}."
            )
        except Exception as e:
            return f"Failed to process PR: {str(e)}"