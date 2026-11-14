import { A2uiSurface, A2uiLitElement } from '@a2ui/lit';
import { Catalog, MessageProcessor } from '@a2ui/web_core';
import { ChildListSchema } from '@a2ui/web_core/v1_0';
import { html } from 'lit';
import { z } from 'zod';

const id = 'urn:codex:visual-explanations:1';
const stack = { name: 'Stack', schema: z.object({ children: ChildListSchema }).strict(), tagName: 've-stack' };

class Stack extends A2uiLitElement {
  api = stack;

  createRenderRoot() {
    return this;
  }

  render() {
    const children = this.controller?.props?.children;
    return children?.map(child => this.renderNode(child));
  }
}

function registerOutlet(name, tagName, content) {
  const api = { name, tagName, schema: z.object({}).strict() };
  class Outlet extends A2uiLitElement {
    createRenderRoot() {
      return this;
    }

    render() {
      return content;
    }
  }
  customElements.define(tagName, Outlet);
  return api;
}

customElements.define(stack.tagName, Stack);
const catalog = new Catalog(id, '1.0', [
  stack,
  registerOutlet('Steps', 've-steps', html`<div data-visual-controls></div>`),
  registerOutlet('Mechanism', 've-mechanism', html`<div data-visual-plot></div>`),
  registerOutlet('ObjectDetail', 've-detail', html`<div data-visual-detail></div>`),
]);

export async function mount(host, message) {
  const processor = new MessageProcessor([catalog]);
  const errors = processor.processMessagesWithErrors([message]);
  const model = processor.getSurface(message.createSurface.surfaceId);
  if (errors.length || !model) {
    processor.dispose();
    throw new Error(JSON.stringify(errors) || 'Missing surface');
  }
  const surface = new A2uiSurface();
  surface.surface = model;
  host.append(surface);
  await surface.updateComplete;
  const layout = surface.querySelector('ve-stack');
  await layout?.updateComplete;
  const outlets = [...surface.querySelectorAll('ve-steps,ve-mechanism,ve-detail')];
  await Promise.all(outlets.map(element => element.updateComplete));
  const targets = {
    controls: surface.querySelector('[data-visual-controls]'),
    plot: surface.querySelector('[data-visual-plot]'),
    detail: surface.querySelector('[data-visual-detail]'),
  };
  if (!targets.controls || !targets.plot || !targets.detail) {
    processor.dispose();
    throw new Error('The layout did not mount its three registered components');
  }
  // A math surface's plot, controls and readout use one local state path;
  // the approved scene input stays immutable. No agent actions or network.
  const data = model.dataModel;
  return { targets, scene: data.get('/scene'), interaction: {
    get: () => data.get('/interaction'),
    set: value => data.set('/interaction', value),
    subscribe: listener => data.subscribe('/interaction', listener),
  } };
}
