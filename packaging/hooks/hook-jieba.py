"""Collect only jieba's required core dictionary, not optional large corpora."""

from PyInstaller.utils.hooks import collect_data_files


datas = collect_data_files("jieba", includes=["dict.txt"])
