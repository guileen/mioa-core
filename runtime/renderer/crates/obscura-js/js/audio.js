// Pure-JS graph/DSP backend: web-audio-engine 0.13.4 (MIT). No fingerprint
// samples, random perturbations, or target-normalized output are used.
(function installAudio() {
  const implementations = new WeakMap();
  const _audioGetImpl = WeakMap.prototype.get.bind(implementations);
  const _audioSetImpl = WeakMap.prototype.set.bind(implementations);
  const contexts = new WeakMap();
  const _audioGetContext = WeakMap.prototype.get.bind(contexts);
  const _audioSetContext = WeakMap.prototype.set.bind(contexts);
  const clocks = new WeakMap();
  const getClock = WeakMap.prototype.get.bind(clocks);
  const setClock = WeakMap.prototype.set.bind(clocks);
  const typedArrayTag = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(Float32Array.prototype), Symbol.toStringTag).get;
  const float32Kind = Function.prototype.call.bind(typedArrayTag);
  const typedSet = Function.prototype.call.bind(Float32Array.prototype.set);
  const typedSubarray = Function.prototype.call.bind(Float32Array.prototype.subarray);
  const module = { exports: {} };
  const exports = module.exports;
  /* WEB_AUDIO_ENGINE */
  const engine = module.exports;
  const Base = engine.api.BaseAudioContext;
  const notSupported = feature => { throw new DOMException(feature + ' is not supported by this audio backend', 'NotSupportedError'); };
  const validRate = value => {
    const rate = Number(value);
    if (!Number.isFinite(rate) || rate < 8000 || rate > 96000) notSupported('Sample rates outside 8000..96000 Hz');
    return rate;
  };
  const validChannels = value => {
    const channels = Number(value);
    if (!Number.isInteger(channels) || channels < 1 || channels > 32) throw new DOMException('Invalid channel count', 'NotSupportedError');
    return channels;
  };
  const validLength = value => {
    const length = Number(value);
    if (!Number.isInteger(length) || length <= 0 || length > 0x7fffffff) throw new DOMException('Invalid buffer length', 'NotSupportedError');
    return length;
  };
  const bufferPrototype = engine.api.AudioBuffer.prototype;
  const channelIndex = (buffer, value) => {
    const implementation = _audioGetImpl(buffer);
    if (!(implementation instanceof engine.impl.AudioBuffer)) throw new TypeError('AudioBuffer required');
    const channel = Number(value) >>> 0;
    if (channel >= implementation.getNumberOfChannels()) throw new DOMException('Channel index is out of bounds', 'IndexSizeError');
    return channel;
  };
  const originalChannelData = bufferPrototype.getChannelData;
  bufferPrototype.getChannelData = function getChannelData(channel) {
    if (arguments.length < 1) throw new TypeError('getChannelData requires a channel');
    return originalChannelData.call(this, channelIndex(this, channel));
  };
  for (const method of ['copyFromChannel', 'copyToChannel']) {
    bufferPrototype[method] = function(array, channel, offset = 0) {
      if (arguments.length < 2 || float32Kind(array) !== 'Float32Array') throw new TypeError(method + ' requires Float32Array and channel');
      const data = originalChannelData.call(this, channelIndex(this, channel));
      offset = Number(offset) >>> 0;
      if (offset >= data.length) return;
      const count = Math.min(array.length, data.length - offset);
      if (method === 'copyFromChannel') typedSet(array, typedSubarray(data, offset, offset + count));
      else typedSet(data, typedSubarray(array, 0, count), offset);
    };
  }
  // Guard the actual scheduled source once, including buffer-source's own
  // start overload. Rejected calls do not alter the DSP schedule.
  const started = new WeakSet();
  const hasStarted = WeakSet.prototype.has.bind(started);
  const setStarted = WeakSet.prototype.add.bind(started);
  for (const prototype of [engine.api.AudioScheduledSourceNode.prototype, engine.api.AudioBufferSourceNode.prototype]) {
    const start = prototype.start;
    prototype.start = function startSource(when = 0, ...args) {
      const implementation = _audioGetImpl(this);
      if (!implementation || typeof implementation.start !== 'function') throw new TypeError('AudioScheduledSourceNode required');
      if (hasStarted(this)) throw new DOMException('Source has already been started', 'InvalidStateError');
      when = Number(when);
      if (!Number.isFinite(when)) throw new TypeError('Start time must be finite');
      if (when < 0) throw new RangeError('Start time must be nonnegative');
      const result = start.call(this, when, ...args);
      setStarted(this);
      return result;
    };
  }
  engine.api.AudioNode.prototype.disconnect = function disconnect(destination, output, input) {
    const source = _audioGetImpl(this);
    if (!(source instanceof engine.impl.AudioNode)) throw new TypeError('AudioNode required');
    if (arguments.length === 0) return source.disconnect();
    if (typeof destination === 'number') {
      const port = Number(destination) >>> 0;
      if (port >= source.outputs.length) throw new DOMException('Invalid output port', 'IndexSizeError');
      return source.disconnect(port);
    }
    const target = _audioGetImpl(destination);
    if (!(target instanceof engine.impl.AudioNode) && !(target instanceof engine.impl.AudioParam)) throw new TypeError('Audio destination required');
    if (target.context !== source.context) throw new DOMException('Destination belongs to another context', 'InvalidAccessError');
    const selectedOutput = arguments.length > 1 ? Number(output) >>> 0 : null;
    if (selectedOutput !== null && selectedOutput >= source.outputs.length) throw new DOMException('Invalid output port', 'IndexSizeError');
    const selectedInput = arguments.length > 2 ? Number(input) >>> 0 : null;
    if (selectedInput !== null && target instanceof engine.impl.AudioParam) throw new TypeError('AudioParam disconnect has no input port');
    if (selectedInput !== null && selectedInput >= target.inputs.length) throw new DOMException('Invalid input port', 'IndexSizeError');
    const connected = source.outputs.some((port, index) =>
      (selectedOutput === null || selectedOutput === index)
      && port.inputs.some(connection => connection.node === target && (selectedInput === null || connection.index === selectedInput)));
    if (!connected) throw new DOMException('Destination is not connected', 'InvalidAccessError');
    if (selectedOutput === null) return source.disconnect(target);
    if (selectedInput === null) return source.outputs[selectedOutput].disconnect(target);
    return source.disconnect(target, selectedOutput, selectedInput);
  };
  // Upstream's compressor is a passthrough. Replace it with linked-channel
  // soft-knee compression and per-sample attack/release gain smoothing. This is
  // real compression, not the platform-specific WebKit look-ahead algorithm.
  const compressor = engine.impl.DynamicsCompressorNode.prototype;
  compressor.getReduction = function() { return this._gainReduction || 0; };
  compressor.dspProcess = function() {
    const input = this.inputs[0].bus.getChannelData();
    const output = this.outputs[0].bus.getMutableData();
    const threshold = Math.max(-100, Math.min(0, this._threshold.getValue()));
    const knee = Math.max(0, Math.min(40, this._knee.getValue()));
    const ratio = Math.max(1, Math.min(20, this._ratio.getValue()));
    const attack = Math.max(0, Math.min(1, this._attack.getValue()));
    const release = Math.max(0, Math.min(1, this._release.getValue()));
    const a = attack === 0 ? 0 : Math.exp(-1 / (attack * this.sampleRate));
    const r = release === 0 ? 0 : Math.exp(-1 / (release * this.sampleRate));
    let reduction = this._gainReduction || 0;
    for (let i = 0; i < this.blockSize; i++) {
      let peak = 0;
      for (let ch = 0; ch < input.length; ch++) peak = Math.max(peak, Math.abs(input[ch][i]));
      const level = 20 * Math.log10(Math.max(1e-20, peak));
      const over = level - threshold;
      let desired = 0;
      if (knee > 0 && over > -knee / 2 && over < knee / 2) {
        desired = (1 / ratio - 1) * (over + knee / 2) ** 2 / (2 * knee);
      } else if (over >= knee / 2) desired = (1 / ratio - 1) * over;
      const coefficient = desired < reduction ? a : r;
      reduction = coefficient * reduction + (1 - coefficient) * desired;
      const gain = Math.pow(10, reduction / 20);
      for (let ch = 0; ch < output.length; ch++) output[ch][i] = (input[ch] || input[0])[i] * gain;
    }
    this._gainReduction = reduction;
  };
  // These upstream node implementations are passthroughs, not DSP. Reject them
  // rather than advertise successful processing that does not happen.
  for (const name of ['createConvolver', 'createPanner', 'createSpatialPanner']) {
    Object.defineProperty(Base.prototype, name, { value: function() { return notSupported(name); }, configurable: true });
  }
  const originalBuffer = Base.prototype.createBuffer;
  Base.prototype.createBuffer = function(channels, length, rate) {
    return originalBuffer.call(this, validChannels(channels), validLength(length), validRate(rate));
  };
  class AudioContext extends Base {
    constructor(options = {}) {
      const rate = validRate(options.sampleRate === undefined ? 44100 : options.sampleRate);
      if (options.sinkId !== undefined && options.sinkId !== '') notSupported('Audio output devices');
      super({ sampleRate: rate, blockSize: 128, numberOfChannels: 2 });
      setClock(this, {buffers: [new Float32Array(128), new Float32Array(128)], timer: null, origin: 0});
      _audioGetImpl(this).changeState('running');
      this._startClock();
    }
    _startClock() {
      const clock = getClock(this);
      if (!clock) throw new TypeError('AudioContext required');
      if (clock.timer !== null || this.state !== 'running') return;
      clock.origin = performance.now() - this.currentTime * 1000;
      clock.timer = setInterval(() => {
        const until = (performance.now() - clock.origin) / 1000;
        while (this.state === 'running' && this.currentTime < until) {
          const before = this.currentTime;
          _audioGetImpl(this).process(clock.buffers, 0);
          // ScriptProcessor callbacks may synchronously suspend/close. Never
          // spin if the real DSP stops advancing its sample clock.
          if (this.currentTime <= before) break;
        }
      }, 20);
    }
    get baseLatency() { return 128 / this.sampleRate; }
    get outputLatency() { return 0; } // Headless sink: no physical output device.
    resume() { return super.resume().then(() => this._startClock()); }
    suspend() { const clock = getClock(this); clearInterval(clock.timer); clock.timer = null; return super.suspend(); }
    close() { const clock = getClock(this); clearInterval(clock.timer); clock.timer = null; return super.close(); }
    createMediaElementSource() { return notSupported('Media element input'); }
    createMediaStreamSource() { return notSupported('Media stream input'); }
    createMediaStreamDestination() { return notSupported('Media stream output'); }
    setSinkId() { return Promise.reject(new DOMException('No physical audio sink', 'NotSupportedError')); }
  }
  class OfflineAudioContext extends engine.OfflineAudioContext {
    constructor(channels, length, rate) {
      if (channels && typeof channels === 'object') {
        const options = channels;
        channels = options.numberOfChannels === undefined ? 1 : options.numberOfChannels;
        length = options.length;
        rate = options.sampleRate;
      }
      super(validChannels(channels), validLength(length), validRate(rate));
    }
  }
  globalThis.AudioContext = AudioContext;
  globalThis.OfflineAudioContext = OfflineAudioContext;
  globalThis.webkitAudioContext = AudioContext;
  globalThis.AudioBuffer = engine.api.AudioBuffer;
  for (const name of ['BaseAudioContext', 'AudioNode', 'AudioParam', 'AudioDestinationNode', 'AudioScheduledSourceNode', 'AudioBufferSourceNode',
    'OscillatorNode', 'GainNode', 'BiquadFilterNode', 'IIRFilterNode', 'AnalyserNode', 'DynamicsCompressorNode',
    'DelayNode', 'WaveShaperNode', 'StereoPannerNode', 'ChannelSplitterNode', 'ChannelMergerNode',
    'ConstantSourceNode', 'PeriodicWave', 'ScriptProcessorNode']) {
    if (engine.api[name]) Object.defineProperty(globalThis, name, { value: engine.api[name], writable: true, configurable: true });
  }
  for (const name of ['AudioContext', 'OfflineAudioContext', 'AudioBuffer', ...Object.keys(engine.api)]) {
    const Interface = globalThis[name];
    if (typeof Interface === 'function' && Interface.prototype) {
      Object.defineProperty(Interface.prototype, Symbol.toStringTag, { value: name, configurable: true });
    }
  }
})();
