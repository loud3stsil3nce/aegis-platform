def validate_command(command: str):
    """Simple security guardrail to prevent arbitrary command execution."""
    ALLOWED_COMMANDS = ['free', 'df', 'docker']
    
    # Check if the command is in our allow-list
    if not any(cmd in command for cmd in ALLOWED_COMMANDS):
        raise PermissionError(f"Security Alert: Command '{command}' is not permitted.")
    return True