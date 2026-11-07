onmessage=async()=>{
  try {
    const canvas=new OffscreenCanvas(32,24);const ctx=canvas.getContext('2d');ctx.fillStyle='red';ctx.fillRect(0,0,4,4);
    const pixel=Array.from(ctx.getImageData(1,1,1,1).data).join();ctx.font='16px sans-serif';
    const metrics=ctx.measureText('WWWW');const narrow=ctx.measureText('iiii').width;ctx.fillText('Wi',2,20);
    const textPainted=ctx.getImageData(0,6,32,18).data.some((value,index)=>index%4===3&&value>0);
    const gradient=ctx.createConicGradient(0,8,8);gradient.addColorStop(0,'red');gradient.addColorStop(1,'blue');
    const path=new Path2D();path.rect(20,0,4,4);ctx.fillStyle=gradient;ctx.fill(path);
    const blob=await canvas.convertToBlob();const bytes=await blob.arrayBuffer();const view=new DataView(bytes);
    const functional=new OffscreenCanvas(1,1),functionalContext=functional.getContext('2d');
    functionalContext.fillStyle='rgba(255,0,0,0.5)';functionalContext.fillRect(0,0,1,1);
    if(Array.from(functionalContext.getImageData(0,0,1,1).data).join()!=='255,0,0,128')throw new Error('native Worker rgba paint/readback failed');
    const reference=new OffscreenCanvas(1,1),referenceContext=reference.getContext('2d');
    referenceContext.fillStyle='#ff000080';referenceContext.fillRect(0,0,1,1);
    const functionalPng=new Uint8Array(await (await functional.convertToBlob()).arrayBuffer());
    const referencePng=new Uint8Array(await (await reference.convertToBlob()).arrayBuffer());
    if(functionalPng.join()!==referencePng.join())throw new Error('native Worker rgba PNG differs from equivalent hex pixels');
    const gpuCanvas=new OffscreenCanvas(8,8);const gl=gpuCanvas.getContext('webgl');
    if(!gl)throw new Error('required Worker WebGL backend unavailable');
    if(gl.DITHER!==0x0BD0)throw new Error('standard DITHER capability missing');
    gl.disable(gl.DITHER);if(gl.getError()!==gl.NO_ERROR)throw new Error('native DITHER disable failed');
    gl.enable(gl.DITHER);if(gl.getError()!==gl.NO_ERROR)throw new Error('native DITHER enable failed');
    const rgba=new Uint8Array(4);
    const read=()=>{gl.readPixels(2,2,1,1,gl.RGBA,gl.UNSIGNED_BYTE,rgba);return Array.from(rgba).join();};
    gl.clearColor(0,1,0,1);gl.clear(gl.COLOR_BUFFER_BIT);const gpuClear=read();
    const shader=(type,source)=>{const shader=gl.createShader(type);gl.shaderSource(shader,source);gl.compileShader(shader);if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(shader));return shader;};
    const program=gl.createProgram();
    gl.attachShader(program,shader(gl.VERTEX_SHADER,'attribute vec2 p; uniform mat4 m; void main(){gl_Position=m*vec4(p,0.0,1.0);}'));
    gl.attachShader(program,shader(gl.FRAGMENT_SHADER,'precision mediump float; uniform vec4 color; void main(){gl_FragColor=color;}'));
    gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(program));gl.useProgram(program);
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
    const position=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(position);gl.vertexAttribPointer(position,2,gl.FLOAT,false,0,0);
    gl.uniformMatrix4fv(gl.getUniformLocation(program,'m'),false,new Float32Array([1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]));
    const color=gl.getUniformLocation(program,'color');gl.uniform4f(color,1,0,0,1);gl.drawArrays(gl.TRIANGLES,0,3);const gpuDraw=read();
    const gpuBitmap=gpuCanvas.transferToImageBitmap();const gpuAfterTransfer=read();
    const copied=new OffscreenCanvas(8,8).getContext('2d');copied.drawImage(gpuBitmap,0,0);const gpuSnapshot=Array.from(copied.getImageData(2,2,1,1).data).join();
    gl.uniform4f(color,0,0,1,1);gl.drawArrays(gl.TRIANGLES,0,3);const gpuStateRetained=read();
    const result={document:typeof document,window:typeof window,canvasBrand:canvas instanceof OffscreenCanvas,contextBrand:ctx instanceof OffscreenCanvasRenderingContext2D,
      gradientBrand:gradient instanceof CanvasGradient,pathBrand:path instanceof Path2D,metricsBrand:metrics instanceof TextMetrics,blobBrand:blob instanceof Blob,
      pixel,wide:metrics.width,narrow,textPainted,pngWidth:view.getUint32(16),pngHeight:view.getUint32(20),
      gpuBrand:gl instanceof WebGLRenderingContext,bitmapBrand:gpuBitmap instanceof ImageBitmap,gpuClear,gpuDraw,gpuAfterTransfer,gpuSnapshot,gpuStateRetained};
    canvas.width=32;result.afterResize=ctx.getImageData(1,1,1,1).data[3];postMessage(result);
  }catch(error){postMessage({error:String(error),stack:error.stack});}
};
