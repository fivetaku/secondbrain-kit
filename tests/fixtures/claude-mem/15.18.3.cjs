// trimmed from claude-mem@15.18.3 plugin/scripts/worker-service.cjs (definitions + buildLauncherPrefix only)
var Gq="chroma-mcp",EYt=2048;var Vq="0.2.6",sM=["onnxruntime>=1.20","protobuf<7","chromadb==1.5.9","pydantic<2.14"],wYt=sM.find(t=>t.startsWith("chromadb=="))?.slice(10)??null;
class xo{static x(){}static buildLauncherPrefix(e){let r=sM.flatMap(n=>["--with",n]);return["--python",e,...r,"--from",`chroma-mcp==${Vq}`,"chroma-mcp"]}static buildPrewarmCommandArgs(e){return e}}
