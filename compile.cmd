@for /f "delims=" %%i in ('python %~dp0sys_name.py') do @set SUFFIX=%%i
@cd /d "%~dp0"
@cl /EHsc /Fe:out.exe /std:c++20 /MT /O3 server.cpp /link /MANIFEST:EMBED /subsystem:windows /entry:mainCRTStartup /OPT:REF /OPT:ICF /LTCG
@del /f /s /q rmpsm_server.%SUFFIX% 2>nul
@move out.exe "rmpsm_server.%SUFFIX%" && del /f /s /q server.obj 2>nul
