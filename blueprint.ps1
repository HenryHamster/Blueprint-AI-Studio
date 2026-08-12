$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$cliScript = Join-Path $repoRoot "scripts\blueprint.py"
$pythonCommand = $null
$pythonArgs = @()

if ($env:BLUEPRINT_PYTHON) {
    $explicit = Get-Command -Name $env:BLUEPRINT_PYTHON -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($explicit) {
        $pythonCommand = $explicit.Source
    } elseif (Test-Path -LiteralPath $env:BLUEPRINT_PYTHON) {
        $pythonCommand = (Resolve-Path -LiteralPath $env:BLUEPRINT_PYTHON).Path
    } else {
        Write-Error "BLUEPRINT_PYTHON does not name an executable: $env:BLUEPRINT_PYTHON"
        exit 1
    }
}

if (-not $pythonCommand) {
    $repoPython = Join-Path $repoRoot "backend\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $repoPython) {
        $pythonCommand = $repoPython
    }
}

# Linked worktrees normally do not duplicate the backend virtual environment.
# When this checkout lives under <repo>\.worktrees\<name> or
# <repo>\.codex-worktrees\<name>, reuse the main checkout's interpreter before
# falling back to PATH.
if (-not $pythonCommand) {
    $worktreesRoot = Split-Path -Parent $repoRoot
    $worktreesDirectory = Split-Path -Leaf $worktreesRoot
    if ($worktreesDirectory -in @(".worktrees", ".codex-worktrees")) {
        $sharedRepoRoot = Split-Path -Parent $worktreesRoot
        $sharedRepoPython = Join-Path $sharedRepoRoot "backend\.venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $sharedRepoPython) {
            $pythonCommand = $sharedRepoPython
        }
    }
}

if (-not $pythonCommand) {
    $python = Get-Command -Name "python" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($python) {
        $pythonCommand = $python.Source
    }
}

if (-not $pythonCommand) {
    $pyLauncher = Get-Command -Name "py" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($pyLauncher) {
        $pythonCommand = $pyLauncher.Source
        $pythonArgs = @("-3")
    }
}

if (-not $pythonCommand) {
    Write-Error (
        "Python 3 was not found. Create backend\.venv, install Python, or set " +
        "BLUEPRINT_PYTHON to the interpreter path."
    )
    exit 1
}

& $pythonCommand @pythonArgs $cliScript @args
exit $LASTEXITCODE
