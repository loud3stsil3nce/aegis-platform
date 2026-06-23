import os
import json
from typing import Annotated
from github import Github, UnknownObjectException
from fastmcp import FastMCP
from src.db.database import async_session
from src.db.models import AuditTrail
from sqlalchemy import select
import contextvars

active_issue_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_issue_key", default=None)


GITHUB_PAT = os.getenv("GITHUB_PAT")

def get_github_client():                                                                                      
    if not GITHUB_PAT:                                                                                        
        raise ValueError("Configuration Error: GITHUB_PAT environment variable is not set.")                  
    return Github(GITHUB_PAT)  

def register_vcs_tools(mcp: FastMCP):                                                                                                                                                     
                                                                                                                                                                                              
    @mcp.tool()                                                                                                                                                                           
    def get_file_from_api(
        repo_name: Annotated[str, "The GitHub repository name in 'owner/repo' format (e.g. 'loud3stsil3nce/aegis-platform')"],
        path: Annotated[str, "The file path within the repository (e.g. 'services/sre-agent/server.py')"],
        ref: Annotated[str, "The branch name, commit SHA, or tag to fetch from"] = "main"
    ) -> str:                                                                                                           
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
    def create_branch(
        repo_name: Annotated[str, "The GitHub repository name in 'owner/repo' format (e.g. 'loud3stsil3nce/aegis-platform')"],
        new_branch: Annotated[str, "The name of the new branch to create (e.g. 'feature/diagnostics')"],
        base_branch: Annotated[str, "The name of the base branch to branch off of"] = "main"
    ) -> str:                                                                                                 
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
    def commit_file_change(
        repo_name: Annotated[str, "The GitHub repository name in 'owner/repo' format (e.g. 'loud3stsil3nce/aegis-platform')"],
        path: Annotated[str, "The file path within the repository to modify or create"],
        new_content: Annotated[str, "The full content to write to the file"],
        commit_message: Annotated[str, "The commit message"],
        branch: Annotated[str | None, "The branch name to commit the change to (falls back to default branch if not specified)"] = None
    ) -> str:
        """
        Modifies or creates a file in-memory and commits it directly to a branch via REST API.
        """
        try:
            g = get_github_client()
            repo = g.get_repo(repo_name)
            
            # Determine target branch (fallback to issue-specific or generic branch if not specified)
            issue_key = active_issue_key.get()
            if branch:
                target_branch = branch
            elif issue_key:
                target_branch = f"issue/{issue_key}-sre-changes"
            else:
                target_branch = "SRE_Agent"
            
            # Check if target branch exists. If not, create it from the default branch.
            try:
                repo.get_branch(target_branch)
            except UnknownObjectException:
                if target_branch == repo.default_branch:
                    return f"Failed to commit file change: default branch '{target_branch}' does not exist."
                try:
                    base_ref = repo.get_branch(repo.default_branch)
                    repo.create_git_ref(ref=f"refs/heads/{target_branch}", sha=base_ref.commit.sha)
                except Exception as create_branch_err:
                    return f"Failed to create branch '{target_branch}': {str(create_branch_err)}"
            
            # Retrieve file if it exists, to get its SHA for update
            try:
                contents = repo.get_contents(path, ref=target_branch)
                # File exists, so update it
                repo.update_file(
                    path=path,
                    message=commit_message,
                    content=new_content,
                    sha=contents.sha,
                    branch=target_branch
                )
                return f"✅ File '{path}' successfully modified and committed to branch '{target_branch}'."
            except UnknownObjectException:
                # File does not exist, so create it
                repo.create_file(
                    path=path,
                    message=commit_message,
                    content=new_content,
                    branch=target_branch
                )
                return f"✅ File '{path}' successfully created and committed to branch '{target_branch}'."
        except Exception as e:
            return f"Failed to commit file change: {str(e)}"

    @mcp.tool()                                                                                                                                                                           
    async def create_pr(
        repo_name: Annotated[str, "The GitHub repository name in 'owner/repo' format (e.g. 'loud3stsil3nce/aegis-platform')"],
        title: Annotated[str, "The title of the pull request"],
        body: Annotated[str, "The detailed description or body of the pull request"],
        head_branch: Annotated[str, "The source branch containing the changes"],
        base_branch: Annotated[str, "The destination branch to merge into"] = "main",
        approved_action_id: Annotated[int, "The SRE Audit Trail Action ID that has been approved (optional)"] = None
    ) -> str:                                                                                                      
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

    @mcp.tool()
    def list_branches(
        repo_name: Annotated[str, "The GitHub repository name in 'owner/repo' format (e.g. 'loud3stsil3nce/aegis-platform')"]
    ) -> str:
        """
        Lists all branches in the specified GitHub repository.
        """
        try:
            g = get_github_client()
            repo = g.get_repo(repo_name)
            branches = [b.name for b in repo.get_branches()]
            if not branches:
                return f"No branches found in repository '{repo_name}'."
            return f"Branches in '{repo_name}':\n" + "\n".join(f"- {b}" for b in branches)
        except Exception as e:
            return f"Failed to list branches: {str(e)}"

    @mcp.tool()
    def list_files_in_branch(
        repo_name: Annotated[str, "The GitHub repository name in 'owner/repo' format (e.g. 'loud3stsil3nce/aegis-platform')"],
        ref: Annotated[str, "The branch name, commit SHA, or tag to list files from-- by default it is main unless it is specified otherwise"] = "main",
        path_filter: Annotated[str | None, "If specified, only paths containing this substring (case-insensitive) will be returned"] = None
    ) -> str:
        """
        Recursively lists all files within a specific branch or ref in the repository.
        Can optionally filter results by path substring.
        """
        try:
            g = get_github_client()
            repo = g.get_repo(repo_name)
            
            # Resolve the reference/branch to get the commit SHA
            try:
                branch = repo.get_branch(ref)
                sha = branch.commit.sha
            except UnknownObjectException:
                # If it's not a branch, try accessing it directly as a ref/commit/tag
                try:
                    commit = repo.get_commit(ref)
                    sha = commit.sha
                except Exception:
                    return f"Failed to find branch or ref '{ref}' in repository '{repo_name}'."
            
            # Retrieve the recursive tree
            tree = repo.get_git_tree(sha=sha, recursive=True)
            
            # Filter and collect files (blobs)
            files = []
            for element in tree.tree:
                if element.type == 'blob':  # 'blob' is a file
                    path = element.path
                    if path_filter:
                        if path_filter.lower() in path.lower():
                            files.append(path)
                    else:
                        files.append(path)
            
            if not files:
                return f"No files found in '{ref}' matching the filter." if path_filter else f"No files found in '{ref}'."
            
            # Format and limit results to prevent context overflow
            max_results = 200
            total_count = len(files)
            truncated = False
            if total_count > max_results:
                files = files[:max_results]
                truncated = True
                
            result_str = f"Found {total_count} files in reference '{ref}':\n"
            result_str += "\n".join(f"- {f}" for f in files)
            if truncated:
                result_str += f"\n\n... and {total_count - max_results} more files. Use a path_filter to narrow down the search."
                
            return result_str
        except Exception as e:
            return f"Failed to list files: {str(e)}"