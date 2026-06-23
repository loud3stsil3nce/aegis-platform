# SRE Agent

The SRE Agent is a critical component of the Aegis Platform, functioning as an autonomous operations and diagnostic agent. Designed to integrate seamlessly with the platform's infrastructure, the SRE Agent performs ongoing health checks, audits, and compliance verifications, while also executing deployments and updates.

## Key Features
- **Autonomous Monitoring**: Continuously checks the health of the platform’s containers and other resources.
- **Compliance and Audit**: Interfaces with APIs to ensure compliance with predefined standards, logging all activity in `db_sre`.
- **GitOps Integration**: Manages source control workflows through APIs without direct git cloning, enhancing security and efficiency.
- **Interaction with Docker Daemon**: Uses Docker socket binding to interact directly with the Docker daemon for container management.

## Environment Setup
Ensure the following environment variables are configured in the `.env` file at the project root:

```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here
OPENAI_API_KEY=your_openai_api_key_here
JIRA_URL=your_jira_instance_url
JIRA_USER_EMAIL=your_email@example.com
JIRA_API_TOKEN=your_api_token_here
JIRA_PROJECT_KEY=KAN # Example project key
GITHUB_PAT=your_personal_access_token
```

## Running the SRE Agent
To initiate the SRE agent as part of the entire platform setup, execute:

```bash
docker-compose up --build sre-agent
```

This will start the agent alongside other services in the defined Docker network infrastructure.

---

This README file encapsulates important aspects of the SRE Agent's role within the Aegis Platform, serving as a touchstone for developers and operators interacting with it.
