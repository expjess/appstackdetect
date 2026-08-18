"""Ad-hoc runner used for development and tests."""
import json, sys
from app.analyze import analyze_path
for p in sys.argv[1:]:
    r = analyze_path(p)
    print("="*90)
    print(p.rsplit('/',1)[-1])
    print("  verdict :", r['verdict']['summary'])
    print("  fw      :", r['verdict']['framework'], r['verdict']['framework_confidence'], "| expo:", r['verdict']['expo_label'])
    print("  scores  :", r['scores'])
    print("  app     :", {k:v for k,v in r['app'].items() if k in ('package','version_name','bundle_id','abis','dex_files')})
    print("  stack   :", json.dumps(r['stack'])[:400])
    print("  expo    :", json.dumps({k:v for k,v in r['expo'].items() if k not in ('config_plugins','expo_modules','android_manifest_meta_data')})[:600])
    print("  packages:", len(r['packages']), [p['name'] for p in r['packages']][:18])
