<#
.SYNOPSIS
    Run a JARVIS script inside WSL without letting Windows rewrite it first.

.DESCRIPTION
    THE BUG THIS PREVENTS. Invoking WSL with inline code lets the OUTER
    shell expand the string before wsl.exe is ever called:

        wsl.exe -- bash -c 'rm -rf $REPO/logs'

    Git Bash and PowerShell both expand $REPO on the Windows side, where it
    does not exist. Bash inside WSL then receives "rm -rf /logs", or with a
    trailing slash, "cp -r /". Nothing is malformed and nothing errors - the
    command simply runs against the wrong path. This has cost real data
    three times on this project.

    The fix is structural, not careful quoting: never send a shell string
    across the boundary. This wrapper only ever invokes a script BY PATH,
    passing arguments as an argument vector, so there is nothing for either
    shell to interpolate. It refuses -c, -s, and anything else that would
    smuggle inline code back in.

    ASCII ONLY, DELIBERATELY. Windows PowerShell 5.1 reads an unsigned .ps1
    as ANSI unless it carries a UTF-8 BOM, so a stray em dash in a string
    literal becomes a parse error on exactly the machine this is meant to
    run on. Keep every character in this file ASCII.

.PARAMETER Script
    Script path relative to the repository, e.g. scripts/status_jarvis.sh

.PARAMETER Arguments
    Passed through to the script verbatim as separate arguments.

.EXAMPLE
    .\scripts\wsl-run.ps1 scripts/status_jarvis.sh

.EXAMPLE
    Running from a UNC path (\\wsl.localhost\...) puts the file in the
    remote zone, where the default execution policy refuses unsigned
    scripts. Bypass it for the single invocation rather than changing the
    machine's policy:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\wsl-run.ps1 scripts/status_jarvis.sh
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $Script,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments = @(),

    [string] $Distro = 'Ubuntu-24.04',

    # Repository path relative to the Linux home directory. Kept relative so
    # no username is baked in; wsl.exe --cd ~ supplies the rest.
    [string] $RepoRelativeToHome = 'jarvis-trading'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string] $message) {
    Write-Error $message -ErrorAction Continue
    exit 64
}

# Inline code is the entire hazard. Reject every spelling of it rather than
# trusting the caller to have quoted correctly this time.
foreach ($banned in @('-c', '-s', '--command', '-Command')) {
    if ($Script -eq $banned) {
        Fail "wsl-run.ps1 does not accept inline code ($banned). Put the logic in a scripts/*.sh file and pass its path - that is the whole point of this wrapper."
    }
}
if ($Script -match '[;&|`$()<>]') {
    Fail "Script path '$Script' contains shell metacharacters. This wrapper runs a file, not a command line."
}
if ($Script -notmatch '\.sh$') {
    Fail "Script path '$Script' is not a .sh file. Only repository scripts may be run through this wrapper."
}
if ([System.IO.Path]::IsPathRooted($Script) -or $Script -match '^\w:' -or $Script -match '^\\\\') {
    Fail "Script path '$Script' must be repository-relative (e.g. scripts/status_jarvis.sh), not an absolute or UNC path."
}
if ($Script -match '\.\.') {
    Fail "Script path '$Script' must not contain '..'."
}

# Forward slashes only - this path is consumed by bash, not by Windows.
$target = "$RepoRelativeToHome/" + ($Script -replace '\\', '/')

# --cd ~ puts the working directory at the Linux home, so the relative path
# above resolves without hardcoding a username. --exec skips the login shell
# entirely, which is one more shell that cannot rewrite anything.
$wslArgs = @('-d', $Distro, '--cd', '~', '--exec', 'bash', $target) + $Arguments

Write-Verbose ("wsl.exe " + ($wslArgs -join ' '))
& wsl.exe @wslArgs
exit $LASTEXITCODE
