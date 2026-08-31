"""Use Zotero's documented debugger server for a non-Firefox application target."""
import json
import socket
from pathlib import Path

root = Path(__file__).resolve().parent
work = Path(json.loads((root / '.smoke' / 'current.json').read_text(encoding='utf-8'))['work'])
assert work.resolve().is_relative_to((root / '.smoke').resolve())
sock = socket.create_connection(('127.0.0.1',6011),timeout=30)
stream=sock.makefile('rb')
def receive():
    length=b''
    while (char:=stream.read(1))!=b':':
        if not char: raise EOFError()
        length+=char
    return json.loads(stream.read(int(length)))
def command(to,kind,**args):
    data=json.dumps({'to':to,'type':kind,**args}).encode()
    sock.sendall(str(len(data)).encode()+b':'+data)
    while True:
        result=receive()
        if result.get('from')==to: return result
receive()
process=command('root','getProcess',id=0)
descriptor=process.get('processDescriptor')
target=command(descriptor['actor'],'getTarget')['process']
print({'processID': target['processID'], 'url':target['url']})
packages=[str(root/'dist'/'journal-rss-memory-0.1.0.xpi')]
script='''(async()=>{
const zotero=Zotero;
if(!zotero.DataDirectory.dir.includes('.smoke')) throw new Error('Not an isolated smoke profile');
const {AddonManager}=ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
let output=[];
try {
 for(const path of PACKAGES){
  let file=Components.classes['@mozilla.org/file/local;1'].createInstance(Components.interfaces.nsIFile);
  file.initWithPath(path);
  const addon=await AddonManager.installTemporaryAddon(file);
  output.push({id:addon.id,active:addon.isActive});
 }
 const scope={Zotero,Services,Components,IOUtils,PathUtils};
 Services.scriptloader.loadSubScript(SMOKE,scope);
 await scope.startup();
} catch(e){output.push({error:String(e),details:e.additionalErrors,stack:e.stack});}
await IOUtils.writeJSON(OUTPUT,output);
})()'''.replace('PACKAGES',json.dumps(packages)).replace('SMOKE',json.dumps((root/'smoke-bootstrap.js').as_uri())).replace('OUTPUT',json.dumps(str(work/'install-result.json')))
response=command(target['consoleActor'],'evaluateJSAsync',text=script)
print(response)
while True:
    event=receive()
    if event.get('type')=='evaluationResult':
        print({'evaluationReceived':True, 'exception':event.get('exceptionMessage')})
        break
sock.close()
