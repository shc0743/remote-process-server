@echo off
cmd /D /c compile.cmd
cd native\bin\
dir /b
copy * ..\..\
dir /b ..\..\
