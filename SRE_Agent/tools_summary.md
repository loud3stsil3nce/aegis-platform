# Summary of Tools Available to Aegis Platform's Automated SRE and Shariah Compliance Auditor

## Server Health and Container Management

- **check_server_health**: Checks the health of the server, focusing on RAM and Disk utilization.
- **list_containers**: Lists currently running Docker containers.
- **check_app_health**: Checks the health and uptime of the Shariah Compliance Screener container.
- **get_recent_logs**: Fetches the last N lines of logs for debugging.
- **search_logs**: Searches container logs for a specific keyword.
- **restart_container**: Restarts a Docker container if necessary.
- **analyze_latency_p95**: Analyzes logs to calculate the 95th percentile response time.
- **ping_web_app**: Performs a synthetic health check by pinging the web app.
- **kill_container**: Kills a specified Docker container.
- **update_and_restart_app**: Full deployment pipeline including rebuilding and restarting the app stack.
- **update_status_dashboard**: Updates the operational status dashboard with current metrics.

## Version Control and Repository Management

- **get_file_from_api**: Fetches the content of a file from the GitHub repository via REST API.
- **create_branch**: Creates a new branch on GitHub.
- **commit_file_change**: Modifies or creates a file in-memory and commits it to a branch.
- **create_pr**: Submits a Pull Request on GitHub, requiring human approval.
- **list_branches**: Lists all branches in the GitHub repository.
- **list_files_in_branch**: Recursively lists all files in a specific branch.

## Shariah Compliance and Monitoring

- **screener_run_screener_scan**: Performs a Shariah compliance scan on a stock ticker.
- **screener_get_screener_watchlist**: Retrieves all stock tickers on the Shariah audit watchlist.
- **screener_add_to_watchlist**: Adds a new stock ticker to the Shariah audit watchlist.

## Miscellaneous Tools

- **reeftracker_get_aquarium_list**: Retrieves all registered aquariums, including their details.
- **reeftracker_get_aquarium_livestock**: Retrieves the livestock currently inside a specific aquarium.
- **messenger_get_active_connections**: Returns a list of usernames currently connected to the websocket server.
- **messenger_check_encryption_keys**: Verifies the encryption keys for registered users.
- **messenger_get_message_stats**: Returns the total number of encrypted messages stored in the database.