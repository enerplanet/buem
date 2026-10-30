@echo off
setlocal EnableDelayedExpansion

:: BUEM Windows CMD Management Script
:: -----------------------------------------------------------------------------
:: Usage:  setup.bat <command> [options]
::
:: Requires NO hardcoded paths. Uses:
::   %~dp0         - the directory of this .bat file (project root)
::   buem          - console-script installed by conda develop src
::   conda run     - fallback when buem is not yet on PATH
::   docker compose - for container workflows
::
:: Tip: activate your conda environment first for the best experience:
::   conda activate buem_env
:: -----------------------------------------------------------------------------

:: Change to the project root (same directory as this script)
cd /d "%~dp0"

:: Conda environment name (override via: set BUEM_CONDA_ENV=my_env)
if "%BUEM_CONDA_ENV%"=="" set BUEM_CONDA_ENV=buem_env
set ENV_FILE=infrastructure\env\buem_env.yml
set COMPOSE_FILE=infrastructure\container\docker-compose.yml

set COMMAND=%1
if "%COMMAND%"=="" set COMMAND=help

if /i "%COMMAND%"=="help"          goto :cmd_help
if /i "%COMMAND%"=="env-update"    goto :cmd_env_update
if /i "%COMMAND%"=="install"       goto :cmd_install
if /i "%COMMAND%"=="install-dev"   goto :cmd_install_dev
if /i "%COMMAND%"=="validate"      goto :cmd_validate
if /i "%COMMAND%"=="version"       goto :cmd_version
if /i "%COMMAND%"=="run"           goto :cmd_run
if /i "%COMMAND%"=="api"           goto :cmd_api
if /i "%COMMAND%"=="multibuilding" goto :cmd_multibuilding
if /i "%COMMAND%"=="docker-build"  goto :cmd_docker_build
if /i "%COMMAND%"=="docker-up"     goto :cmd_docker_up
if /i "%COMMAND%"=="docker-down"   goto :cmd_docker_down
if /i "%COMMAND%"=="docker-logs"   goto :cmd_docker_logs
if /i "%COMMAND%"=="docker-status" goto :cmd_docker_status
if /i "%COMMAND%"=="docker-shell"  goto :cmd_docker_shell
if /i "%COMMAND%"=="test"          goto :cmd_test
if /i "%COMMAND%"=="clean"         goto :cmd_clean
if /i "%COMMAND%"=="clean-all"     goto :cmd_clean_all

echo Unknown command: %COMMAND%
echo.
goto :cmd_help

:: -- help ---------------------------------------------------------------------
:cmd_help
echo BUEM Windows CMD Commands
echo =========================
echo.
echo Usage: setup.bat ^<command^> [options]
echo.
echo Environment Setup:
echo   env-update       Create/update the conda env from %ENV_FILE%
echo                    (pulls in the latest occupancy/weather, tracked @main)
echo   install          env-update + install BUEM (conda develop src)
echo   install-dev      install + dev extras (pytest, ruff, mypy)
echo   validate         Verify installation and environment paths
echo   version          Print the installed BuEM version
echo.
echo Model Commands:
echo   run              Run the thermal model for a single building
echo   run --plot       Run with result plots
echo   run --milp       Run using the MILP solver (experimental)
echo.
echo API Server:
echo   api              Start Gunicorn API server (production)
echo   api --dev        Start Flask dev server (development only)
echo   api --port 8080  Start on a custom port
echo.
echo Multi-Building:
echo   multibuilding                               Run complete demo (auto-optimised)
echo   multibuilding --test parallel               Parallel processing only
echo   multibuilding --test sequential             Sequential processing only
echo   multibuilding --test comparison             Parallel vs sequential comparison
echo   multibuilding --test benchmark              Comprehensive benchmark suite
echo   multibuilding --test optimize               Auto-find optimal configuration
echo   multibuilding --buildings 20                Process 20 buildings
echo   multibuilding --workers N                   N worker processes (1 to CPU count)
echo   multibuilding --cores N                     Limit to N CPU cores
echo   multibuilding --sequential                  Force sequential (no parallelism)
echo   multibuilding --validate-system             Show system capabilities and valid ranges
echo   multibuilding --quiet                       Reduce logging verbosity
echo   Note: invalid --workers values are reported as errors
echo   Example: setup.bat multibuilding --test parallel --workers 8
echo.
echo Docker Workflows:
echo   docker-build     Build the Docker image
echo   docker-up        Start containers (detached)
echo   docker-down      Stop and remove containers
echo   docker-logs      Follow container logs
echo   docker-status    Show container status
echo   docker-shell     Open a shell inside the running container
echo.
echo Tests:
echo   test             Run the test suite (pytest tests/)
echo.
echo Cleanup:
echo   clean            Stop containers and remove build artefacts
echo   clean-all        Remove containers, volumes, and Docker image
echo.
echo Examples:
echo   setup.bat install
echo   setup.bat run --plot
echo   setup.bat api
echo   setup.bat docker-up
echo.
echo Conda environment: %BUEM_CONDA_ENV%  (override: set BUEM_CONDA_ENV=my_env)
goto :end

:: -- Helper: run a buem subcommand ---------------------------------------------
:run_buem_cmd
:: %* passes remaining args after the command token
where buem >nul 2>&1
if %errorlevel%==0 (
    buem %BUEM_SUBCMD% %BUEM_EXTRA_ARGS%
) else (
    echo   'buem' not found on PATH - falling back to: conda run -n %BUEM_CONDA_ENV%
    conda run -n %BUEM_CONDA_ENV% --no-capture-output buem %BUEM_SUBCMD% %BUEM_EXTRA_ARGS%
)
goto :end

:: -- Commands ------------------------------------------------------------------
:cmd_env_update
call :do_env_update
goto :end

:cmd_install
call :do_env_update
echo Installing BUEM into the conda environment...
conda develop src
echo BUEM installed. Verify with:  setup.bat validate
goto :end

:cmd_install_dev
call :do_env_update
echo Installing BUEM + dev extras into the conda environment...
conda develop src
conda install --name %BUEM_CONDA_ENV% --yes pytest pytest-cov ruff mypy
goto :end

:do_env_update
:: Create the conda env if it doesn't exist yet, otherwise update it in
:: place (--prune removes anything no longer listed). This is what
:: actually pulls in the latest occupancy/weather (tracked @main in
:: %ENV_FILE%) -- re-run this after every "git pull" to stay current.
conda env list | findstr /b /r /c:" *%BUEM_CONDA_ENV% " /c:"^%BUEM_CONDA_ENV% " >nul
if %errorlevel%==0 (
    echo Updating conda environment '%BUEM_CONDA_ENV%' from %ENV_FILE% ...
    conda env update -n %BUEM_CONDA_ENV% -f %ENV_FILE% --prune
) else (
    echo Creating conda environment '%BUEM_CONDA_ENV%' from %ENV_FILE% ...
    conda env create -f %ENV_FILE%
)
exit /b 0

:cmd_validate
set BUEM_SUBCMD=validate
set BUEM_EXTRA_ARGS=
goto :run_buem_cmd

:cmd_version
set BUEM_SUBCMD=version
set BUEM_EXTRA_ARGS=
goto :run_buem_cmd

:cmd_run
set BUEM_SUBCMD=run
:: collect everything after the first argument ("run") as extra args
set BUEM_EXTRA_ARGS=
shift
:collect_run_args
if "%1"=="" goto :run_buem_cmd
set BUEM_EXTRA_ARGS=%BUEM_EXTRA_ARGS% %1
shift
goto :collect_run_args

:cmd_api
set BUEM_SUBCMD=api
set BUEM_EXTRA_ARGS=
shift
:collect_api_args
if "%1"=="" goto :run_buem_cmd
set BUEM_EXTRA_ARGS=%BUEM_EXTRA_ARGS% %1
shift
goto :collect_api_args

:cmd_multibuilding
set BUEM_SUBCMD=multibuilding
set BUEM_EXTRA_ARGS=
shift
:collect_mb_args
if "%1"=="" goto :run_buem_cmd
set BUEM_EXTRA_ARGS=%BUEM_EXTRA_ARGS% %1
shift
goto :collect_mb_args

:cmd_docker_build
echo Building Docker image...
docker compose -f %COMPOSE_FILE% build
goto :end

:cmd_docker_up
echo Starting containers...
docker compose -f %COMPOSE_FILE% up -d
echo API available at http://localhost:5000
goto :end

:cmd_docker_down
echo Stopping containers...
docker compose -f %COMPOSE_FILE% down
goto :end

:cmd_docker_logs
docker compose -f %COMPOSE_FILE% logs -f
goto :end

:cmd_docker_status
docker compose -f %COMPOSE_FILE% ps
goto :end

:cmd_docker_shell
echo Opening shell in the api container...
docker compose -f %COMPOSE_FILE% exec api bash
goto :end

:cmd_test
echo Running test suite...
where pytest >nul 2>&1
if %errorlevel%==0 (
    pytest tests\ -v
) else (
    conda run -n %BUEM_CONDA_ENV% --no-capture-output pytest tests\ -v
)
goto :end

:cmd_clean
echo Cleaning build artefacts...
docker compose -f %COMPOSE_FILE% down 2>nul
if exist "src\buem.egg-info" rmdir /s /q "src\buem.egg-info"
for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
echo Clean complete.
goto :end

:cmd_clean_all
echo Removing containers, volumes, and Docker image...
docker compose -f %COMPOSE_FILE% down -v --rmi all 2>nul
goto :cmd_clean

:end
endlocal
