@for /f "delims=" %%i in ('python %~dp0sys_name.py') do @set SUFFIX=%%i
@cd /d "%~dp0"
@cl /O2 /GL /EHsc /Fe:out.exe /std:c++20 /MT server.cpp /link /MANIFEST:EMBED /subsystem:windows /entry:mainCRTStartup /LTCG /OPT:REF /OPT:ICF
@del /f /s /q rmpsm_server.%SUFFIX% 2>nul
@mkdir native\bin
@move out.exe "native\bin\rmpsm_server.%SUFFIX%" && del /f /s /q server.obj 2>nul
