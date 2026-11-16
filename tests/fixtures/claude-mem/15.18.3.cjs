// trimmed from claude-mem@15.18.3 plugin/scripts/worker-service.cjs (definitions + buildLauncherPrefix only)
var q1=1,sM=["onnxruntime>=1.20","protobuf<7","chromadb==1.5.9","pydantic<2.14"],z2=2;var Vq="0.2.6",y3=3;
class Gn{static x(){}static buildLauncherPrefix(e){let r=sM.flatMap(n=>["--with",n]);return["--python",e,...r,"--from",`chroma-mcp==${Vq}`,"chroma-mcp"]}static buildPrewarmCommandArgs(e){return e}}
