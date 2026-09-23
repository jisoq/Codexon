"""Create a QA-only payload whose hashed desktop executable fails runtime validation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--product',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
shutil.copytree(args.product,args.output)
shutil.copy2(args.output/'CodexonRecovery.exe',args.output/'Codexon.exe')
path=args.output/'build-manifest.json'
manifest=json.loads(path.read_text(encoding='utf-8-sig'))
manifest['sha256']=hashlib.sha256((args.output/'Codexon.exe').read_bytes()).hexdigest()
path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
