#[cfg(feature = "render")]
#[test]
#[ignore = "requires real OBSCURA_ENVIRONMENT_FONT_FILES and allowed proportional plus monospace families"]
fn authenticity_provisioned_fonts_preserve_sans_and_monospace_topology() {
    let files = obscura_render::graphics::explicit_font_files().unwrap()
        .expect("provide the actual profile's pinned font file list");
    assert!(!files.is_empty());
    let mut rt = setup_runtime("<!doctype html><body></body>");
    assert_eq!(rt.evaluate(r#"(() => {
        const a=document.createElement('canvas'),b=document.createElement('canvas');
        a.width=b.width=300; a.height=b.height=64;
        const x=a.getContext('2d'),y=b.getContext('2d');
        x.font='20px sans-serif'; y.font='20px monospace';
        const narrow=x.measureText('iiii').width,wide=x.measureText('WWWW').width;
        const monoNarrow=y.measureText('iiii').width,monoWide=y.measureText('WWWW').width;
        const solid=JSON.stringify(x.measureText('Actual metrics'));
        x.fillStyle=x.createLinearGradient(0,0,300,64);
        const paintIndependent=solid===JSON.stringify(x.measureText('Actual metrics'));
        x.fillStyle=y.fillStyle='#ffffff';
        x.fillText('iiii WWWW',0,32); y.fillText('iiii WWWW',0,32);
        return [narrow>0,wide>narrow,monoNarrow>0,Math.abs(monoNarrow-monoWide)<0.0001,
            paintIndependent,Array.from(x.getImageData(0,0,300,64).data).some((v,i)=>i%4===3&&v>0),
            Array.from(y.getImageData(0,0,300,64).data).some((v,i)=>i%4===3&&v>0),
            a.toDataURL()!==b.toDataURL()];
    })()"#).unwrap(),serde_json::json!([true,true,true,true,true,true,true,true]));
}

#[test]
fn authenticity_software_declaration_same_object_survives_host_refresh() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    rt.set_user_agent("Obscura/0.1.0");
    rt.evaluate("globalThis.savedSoftware = navigator.userAgentData;").unwrap();
    rt.set_user_agent("Example/7.2");
    rt.set_platform("Linux armv8l", "Windows", "15.0.0");
    assert_eq!(rt.evaluate("[savedSoftware === navigator.userAgentData,savedSoftware.toJSON()]").unwrap(),
        serde_json::json!([true,{"brands":[{"brand":"Example","version":"7"}],"mobile":false,"platform":"Linux"}]));
}

// Included in runtime's unit-test module so the probes use its real runtime.
#[cfg(feature = "render")]
#[test]
fn authenticity_rgba_draw_readback_png_and_gradient_measurement() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    assert_eq!(rt.evaluate(r#"(() => {
        const a = document.createElement('canvas'), b = document.createElement('canvas');
        a.width = b.width = 4; a.height = b.height = 4;
        const x = a.getContext('2d'), y = b.getContext('2d');
        x.fillStyle = 'rgba(171,174,149,1)'; x.fillRect(0,0,4,2);
        y.fillStyle = '#abae95'; y.fillRect(0,0,4,2);
        x.fillStyle = 'rgba(171,174,149,0.5)'; x.fillRect(0,2,4,2);
        y.fillStyle = '#abae9580'; y.fillRect(0,2,4,2);
        const pixels = x.getImageData(0,0,4,4).data;
        const measure = () => {
            try { return JSON.stringify(x.measureText('Latin 你好 Ω')); }
            catch (e) { return e.name + ':' + e.message; }
        };
        x.fillStyle = '#fff'; const solidMeasure = measure();
        x.fillStyle = x.createLinearGradient(0,0,4,4);
        return [Array.from(pixels.slice(0,4)),pixels[35],
            Array.from(pixels).join() === Array.from(y.getImageData(0,0,4,4).data).join(),
            a.toDataURL() === b.toDataURL(),measure() === solidMeasure];
    })()"#).unwrap(), serde_json::json!([[171,174,149,255],128,true,true,true]));
}

#[cfg(feature = "render")]
#[test]
fn authenticity_hidden_iframe_owns_real_canvas_and_gl_surfaces() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    assert_eq!(rt.evaluate(r#"(() => {
        const root = document.createElement('canvas'); root.width = root.height = 4;
        const rootGL = root.getContext('webgl');
        rootGL.clearColor(0,1,0,1); rootGL.clear(rootGL.COLOR_BUFFER_BIT);
        const iframe = document.createElement('iframe'); iframe.hidden = true;
        document.body.appendChild(iframe);
        const child = iframe.contentWindow;
        const childResult = JSON.parse(child.eval(`JSON.stringify((() => {
            const canvas = document.createElement('canvas'); canvas.width = canvas.height = 4;
            const context = canvas.getContext('2d');
            context.fillStyle = 'rgba(171,174,149,1)'; context.fillRect(0,0,4,4);
            const glCanvas = document.createElement('canvas'); glCanvas.width = glCanvas.height = 4;
            const gl = glCanvas.getContext('webgl');
            gl.clearColor(1,0,0,1); gl.clear(gl.COLOR_BUFFER_BIT);
            const pixels = new Uint8Array(4); gl.readPixels(0,0,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
            return [Array.from(context.getImageData(0,0,1,1).data),Array.from(pixels),gl.getError()];
        })())`));
        const pixels = new Uint8Array(4); rootGL.readPixels(0,0,1,1,rootGL.RGBA,rootGL.UNSIGNED_BYTE,pixels);
        return [childResult,Array.from(pixels),rootGL.getError()];
    })()"#).unwrap(), serde_json::json!([[[171,174,149,255],[255,0,0,255],0],[0,255,0,255],0]));
}

#[tokio::test(flavor = "current_thread")]
async fn authenticity_worker_software_declaration_is_installed_once() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    rt.set_user_agent("Obscura/0.1.0");
    rt.set_platform("MacIntel", "Windows", "15.0.0");
    rt.execute_script("authenticity-worker-declaration", r#"
        globalThis.__authWorker = null;
        const source = 'onmessage=()=>postMessage([navigator.userAgentData===navigator.userAgentData,navigator.userAgentData.toJSON()]);';
        const url = URL.createObjectURL(new Blob([source], {type:'application/javascript'}));
        const worker = new Worker(url);
        worker.onerror = event => { __authWorker = String(event.message); worker.terminate(); };
        worker.onmessage = event => { __authWorker = event.data; worker.terminate(); URL.revokeObjectURL(url); };
        worker.postMessage(null);
    "#).unwrap();
    rt.run_event_loop_bounded(1_000).await.unwrap();
    assert_eq!(rt.evaluate("__authWorker").unwrap(), serde_json::json!([true,
        {"brands":[{"brand":"Obscura","version":"0"}],"mobile":false,"platform":"macOS"}]));
}

#[tokio::test(flavor = "current_thread")]
async fn authenticity_audio_processing_can_suspend_or_close_without_spinning() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    rt.execute_script("authenticity-audio-clock", r#"
        globalThis.__authClock = [];
        for (const action of ['suspend','close']) {
            const context = new AudioContext();
            const source = context.createOscillator(), processor = context.createScriptProcessor(256,1,1);
            source.connect(processor); processor.connect(context.destination);
            processor.onaudioprocess = () => { context[action](); __authClock.push([action,context.state]); };
            source.start();
        }
        setTimeout(() => __authClock.push('timer'),50);
    "#).unwrap();
    rt.run_event_loop_bounded(150).await.unwrap();
    let observed = rt.evaluate("__authClock").unwrap();
    let observed = observed.as_array().unwrap();
    assert!(observed.contains(&serde_json::json!(["suspend","suspended"])));
    assert!(observed.contains(&serde_json::json!(["close","closed"])));
    assert!(observed.contains(&serde_json::json!("timer")));
}

#[tokio::test(flavor = "current_thread")]
async fn authenticity_audio_targeted_disconnect_changes_real_rendered_graph() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    rt.execute_script("authenticity-audio-disconnect", r#"
        globalThis.__authDisconnect = null;
        async function render(disconnect) {
            const context = new OfflineAudioContext(1,256,44100);
            const oscillator = context.createOscillator(), gain = context.createGain();
            oscillator.connect(gain); gain.connect(context.destination);
            if (disconnect) oscillator.disconnect(gain,0,0);
            oscillator.start();
            const buffer = await context.startRendering();
            return Array.from(buffer.getChannelData(0)).some(value => value !== 0);
        }
        Promise.all([render(false),render(true)]).then(values => __authDisconnect = values);
    "#).unwrap();
    rt.run_event_loop_bounded(150).await.unwrap();
    assert_eq!(rt.evaluate("__authDisconnect").unwrap(), serde_json::json!([true,false]));
}

#[test]
fn authenticity_audio_private_receivers_and_cross_realm_arrays() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    assert_eq!(rt.evaluate(r#"(() => {
        const error = fn => { try { fn(); return 'none'; } catch (e) { return e.name; } };
        const context = new OfflineAudioContext(1,128,44100), buffer = context.createBuffer(1,4,44100);
        const iframe = document.createElement('iframe'); document.body.appendChild(iframe);
        const foreign = new iframe.contentWindow.Float32Array([.25,.5]); buffer.copyToChannel(foreign,0);
        const out = new iframe.contentWindow.Float32Array(2); buffer.copyFromChannel(out,0);
        const forged = {_impl:buffer._impl,numberOfChannels:1};
        const oscillator = context.createOscillator(), gain = context.createGain();
        const absent = error(() => oscillator.disconnect(gain)); oscillator.connect(gain);
        const port = error(() => oscillator.disconnect(gain,99));
        const removed = error(() => oscillator.disconnect(gain)), duplicate = error(() => oscillator.disconnect(gain));
        buffer._impl = {getChannelData:()=>new Float32Array([99])};
        return [Array.from(out),error(() => AudioBuffer.prototype.getChannelData.call(forged,0)),
            buffer.getChannelData(0)[0],Reflect.ownKeys(context).includes('_impl'),
            Reflect.ownKeys(oscillator).includes('_impl'),absent,port,removed,duplicate];
    })()"#).unwrap(), serde_json::json!([[0.25,0.5],"TypeError",0.25,false,false,
        "InvalidAccessError","IndexSizeError","none","InvalidAccessError"]));
}

#[test]
fn authenticity_canvas_operations_reject_forged_receivers() {
    let mut rt = setup_runtime("<!doctype html><body></body>");
    assert_eq!(rt.evaluate(r#"(() => {
        const forged = Object.create(CanvasRenderingContext2D.prototype), results = [];
        for (const name of ['resetTransform','getTransform','getLineDash','getContextAttributes']) {
            try { CanvasRenderingContext2D.prototype[name].call(forged); results.push('none'); }
            catch (e) { results.push(e.name); }
        }
        return results;
    })()"#).unwrap(), serde_json::json!(["TypeError","TypeError","TypeError","TypeError"]));
}
