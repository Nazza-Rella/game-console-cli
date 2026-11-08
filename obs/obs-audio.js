'use strict';
// Plays the capture device's PCM16 stereo (about 32728 Hz) through Web Audio without seams.
// Each AudioBufferSourceNode resamples on its own and leaves a step where two buffers meet, so the
// device rate is converted here once, continuously (4-point Hermite), and the converted buffers are
// placed back to back on exact output sample positions. The device and output clocks drift apart
// slowly; the conversion ratio follows the buffered length by at most 0.2%, which is inaudible.
class ObsAudio {
  constructor(context, destination) {
    this.context = context;
    this.destination = destination;
    // Seconds of audio kept scheduled ahead of playback. Packets arrive about every 33 ms; the margin
    // rides out a stalled page or worker of up to about 0.25 s. The page delays video by the same amount.
    this.target = 0.25;
    this.stats = { underruns: 0, gaps: 0, drops: 0, scheduled: 0 };
    this.reset();
  }

  reset() {
    this.expected = null;
    this.left = new Float32Array(8192);
    this.right = new Float32Array(8192);
    // One leading sample gives the first interpolation its previous point.
    this.length = 1;
    this.position = 1;
    this.left[0] = 0;
    this.right[0] = 0;
    this.nextFrame = 0;
    this.ahead = null;
    this.fadeIn = true;
  }

  // start: device sample index of the first frame in pcm (counted from the capture session start).
  // pcm: Int16Array of interleaved L/R samples. rate: device samples per second.
  // Returns the context time at which the last sample of this push finishes playing, or null.
  push(start, pcm, rate) {
    const rateOut = this.context.sampleRate, now = this.context.currentTime;
    const frames = pcm.length >> 1, end = start + frames;
    if (this.expected === null || this.nextFrame / rateOut < now + 0.005) {
      if (this.expected !== null) this.stats.underruns++;
      this.restart(start, end, rate, now);
    }
    if (end <= this.expected) return this.nextFrame / rateOut;
    let from = this.expected - start;
    if (from < 0) {
      // Samples that never reached us. Short holes are filled with silence to keep the timing.
      if (-from > rate * 0.2) {
        this.stats.underruns++;
        this.restart(start, end, rate, now);
        from = this.expected - start;
      } else {
        this.stats.gaps++;
        this.append(null, 0, -from);
        from = 0;
      }
    }
    this.expected = end;
    const ahead = this.nextFrame / rateOut - now;
    if (ahead > this.target + 0.1) {
      // More is queued than the margin allows: skip this audio until the queue drains back.
      this.stats.drops++;
      this.fadeIn = true;
      return this.nextFrame / rateOut;
    }
    this.append(pcm, from, frames - from);
    this.ahead = this.ahead === null ? ahead : this.ahead + (ahead - this.ahead) * 0.05;
    const adjust = Math.max(-0.002, Math.min(0.002, (this.ahead - this.target) * 0.05));
    this.schedule(rate / rateOut * (1 + adjust));
    return this.nextFrame / rateOut;
  }

  // Start again from the newest target-length audio. When less has arrived yet (the capture has
  // just started), playback starts later by the shortfall so that the full margin is there at once.
  restart(start, end, rate, now) {
    this.reset();
    this.expected = Math.max(start, end - Math.round(this.target * rate));
    const shortfall = Math.max(0, this.target - (end - this.expected) / rate);
    this.nextFrame = Math.ceil((now + 0.01 + shortfall) * this.context.sampleRate);
  }

  append(pcm, from, count) {
    if (this.length + count > this.left.length) {
      const size = Math.max(this.left.length * 2, this.length + count);
      const left = new Float32Array(size), right = new Float32Array(size);
      left.set(this.left.subarray(0, this.length));
      right.set(this.right.subarray(0, this.length));
      this.left = left;
      this.right = right;
    }
    for (let i = 0; i < count; i++) {
      this.left[this.length + i] = pcm ? pcm[(from + i) * 2] / 32768 : 0;
      this.right[this.length + i] = pcm ? pcm[(from + i) * 2 + 1] / 32768 : 0;
    }
    this.length += count;
  }

  schedule(step) {
    const left = this.left, right = this.right;
    const available = Math.floor((this.length - 3 - this.position) / step) + 1;
    if (available <= 0) return;
    const outLeft = new Float32Array(available), outRight = new Float32Array(available);
    let position = this.position;
    for (let n = 0; n < available; n++) {
      const i = Math.floor(position), t = position - i;
      outLeft[n] = hermite(left[i - 1], left[i], left[i + 1], left[i + 2], t);
      outRight[n] = hermite(right[i - 1], right[i], right[i + 1], right[i + 2], t);
      position += step;
    }
    // Keep the sample before the next read position for the next interpolation.
    const consumed = Math.floor(position) - 1;
    left.copyWithin(0, consumed, this.length);
    right.copyWithin(0, consumed, this.length);
    this.length -= consumed;
    this.position = position - consumed;
    if (this.fadeIn) {
      const ramp = Math.min(available, Math.round(this.context.sampleRate * 0.005));
      for (let n = 0; n < ramp; n++) {
        outLeft[n] *= n / ramp;
        outRight[n] *= n / ramp;
      }
      this.fadeIn = false;
    }
    const buffer = this.context.createBuffer(2, available, this.context.sampleRate);
    buffer.copyToChannel(outLeft, 0);
    buffer.copyToChannel(outRight, 1);
    const node = this.context.createBufferSource();
    node.buffer = buffer;
    node.connect(this.destination);
    node.start(this.nextFrame / this.context.sampleRate);
    this.nextFrame += available;
    this.stats.scheduled += available;
  }
}

function hermite(previous, current, next, after, t) {
  const c1 = 0.5 * (next - previous);
  const c2 = previous - 2.5 * current + 2 * next - 0.5 * after;
  const c3 = 0.5 * (after - previous) + 1.5 * (current - next);
  return ((c3 * t + c2) * t + c1) * t + current;
}
