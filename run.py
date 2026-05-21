"""
run.py — coloque este arquivo na RAIZ da pasta bradesco_ouvidoria
e execute assim:

  python run.py --mode full --pages 10
  python run.py --mode full --pages 10 --api-key sk-...
  python run.py --mode report
"""
import sys
import os

# Garante que a pasta raiz do projeto esteja no path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.run_pipeline import run
import argparse

parser = argparse.ArgumentParser(description="Ouvidoria Analytics Pipeline — Bradesco")
parser.add_argument("--mode",     default="full",   choices=["full","classify_only","report"])
parser.add_argument("--pages",    type=int,         default=10)
parser.add_argument("--per-page", type=int,         default=10)
parser.add_argument("--api-key",  default=os.getenv("OPENAI_API_KEY",""))
args = parser.parse_args()

run(mode=args.mode, max_pages=args.pages, per_page=args.per_page, api_key=args.api_key)