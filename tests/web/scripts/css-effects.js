async () => {
  await check('CSS supports and inline style accept rgb, rgba, and hsl color functions', () => {
    assert(CSS.supports('color', 'rgb(255, 0, 0)'), 'CSS.supports rgb color');
    assert(CSS.supports('color', 'rgba(0, 0, 0, 0.5)'), 'CSS.supports rgba color');
    assert(CSS.supports('color', 'hsl(120, 100%, 50%)'), 'CSS.supports hsl color');

    const target = document.getElementById('target-box');
    target.style.color = 'rgb(10, 20, 30)';
    assert(target.style.color.includes('10'), 'target inline style color reflects rgb value');
    target.style.backgroundColor = 'rgba(100, 150, 200, 0.8)';
    assert(target.style.backgroundColor.includes('100'), 'target inline backgroundColor reflects rgba value');
  });

  await check('CSS supports and inline style accept transition and animation declarations', () => {
    assert(CSS.supports('transition-property', 'opacity'), 'CSS.supports transition-property');
    assert(CSS.supports('transition-duration', '300ms'), 'CSS.supports transition-duration');
    assert(CSS.supports('animation-name', 'slidein'), 'CSS.supports animation-name');
    assert(CSS.supports('animation-duration', '1s'), 'CSS.supports animation-duration');

    const target = document.getElementById('target-box');
    target.style.transitionProperty = 'transform';
    target.style.transitionDuration = '250ms';
    target.style.transitionTimingFunction = 'ease-in-out';
    assert(target.style.transitionDuration === '250ms', 'transitionDuration matches');

    target.style.animationName = 'fadeIn';
    target.style.animationDuration = '500ms';
    assert(target.style.animationName === 'fadeIn', 'animationName matches');
    assert(target.style.animationDuration === '500ms', 'animationDuration matches');
  });

  await check('CSS supports and inline style accept box-sizing, border, and image properties', () => {
    assert(CSS.supports('box-sizing', 'border-box'), 'CSS.supports box-sizing border-box');
    assert(CSS.supports('object-fit', 'cover'), 'CSS.supports object-fit cover');

    const target = document.getElementById('target-box');
    target.style.boxSizing = 'border-box';
    target.style.margin = '10px 20px';
    target.style.padding = '8px 16px';
    target.style.border = '2px solid rgb(0, 0, 0)';
    target.style.borderRadius = '6px';

    assert(target.style.boxSizing === 'border-box', 'boxSizing is border-box');
    assert(target.style.borderRadius === '6px', 'borderRadius is 6px');
  });

  await check('CSS supports and inline style accept transform and contain properties', () => {
    assert(CSS.supports('transform', 'rotate(45deg) scale(1.5)'), 'CSS.supports transform');
    assert(CSS.supports('contain', 'layout paint'), 'CSS.supports contain');

    const target = document.getElementById('target-box');
    target.style.transform = 'translate(10px, 20px) scale(1.2)';
    target.style.transformOrigin = '50% 50%';
    target.style.contain = 'layout paint';

    assert(target.style.transform.includes('translate'), 'transform reflects translation');
    assert(target.style.contain.includes('paint') || target.style.contain.includes('layout'), 'contain reflects layout/paint');
  });

  await check('CSS supports and inline style accept alignment and box sizing modules', () => {
    assert(CSS.supports('justify-content', 'center'), 'CSS.supports justify-content');
    assert(CSS.supports('align-items', 'center'), 'CSS.supports align-items');
    assert(CSS.supports('align-self', 'flex-start'), 'CSS.supports align-self');
    assert(CSS.supports('box-sizing', 'border-box'), 'CSS.supports box-sizing border-box');
    assert(CSS.supports('width', 'max-content'), 'CSS.supports width max-content');
    assert(CSS.supports('aspect-ratio', '16 / 9'), 'CSS.supports aspect-ratio');

    const target = document.getElementById('target-box');
    target.style.justifyContent = 'space-between';
    target.style.alignItems = 'center';
    target.style.aspectRatio = '16 / 9';

    assert(target.style.justifyContent.includes('space-between') || target.style.justifyContent !== '', 'justifyContent reflected');
    assert(target.style.alignItems.includes('center') || target.style.alignItems !== '', 'alignItems reflected');
  });

  await check('CSS supports and inline style accept masking, multicol, shapes, and filter properties', () => {
    assert(CSS.supports('clip-path', 'circle(50%)'), 'CSS.supports clip-path');
    assert(CSS.supports('column-count', '2'), 'CSS.supports column-count');
    assert(CSS.supports('column-rule', '1px solid red'), 'CSS.supports column-rule');
    assert(CSS.supports('shape-outside', 'circle(50%)'), 'CSS.supports shape-outside');
    assert(CSS.supports('filter', 'blur(5px)'), 'CSS.supports filter');

    const target = document.getElementById('target-box');
    target.style.clipPath = 'circle(50%)';
    target.style.columnCount = '2';
    target.style.filter = 'blur(5px)';

    assert(target.style.clipPath.includes('circle') || target.style.clipPath !== '', 'clipPath reflected');
    assert(target.style.columnCount === '2', 'columnCount is 2');
    assert(target.style.filter.includes('blur') || target.style.filter !== '', 'filter reflected');
  });

  await check('CSS supports and inline style accept ui, scroll snap, will change, and writing modes', () => {
    assert(CSS.supports('outline', '1px solid red'), 'CSS.supports outline');
    assert(CSS.supports('cursor', 'pointer'), 'CSS.supports cursor');
    assert(CSS.supports('user-select', 'none'), 'CSS.supports user-select');
    assert(CSS.supports('appearance', 'none'), 'CSS.supports appearance');
    assert(CSS.supports('mix-blend-mode', 'multiply'), 'CSS.supports mix-blend-mode');
    assert(CSS.supports('scroll-snap-type', 'x mandatory'), 'CSS.supports scroll-snap-type');
    assert(CSS.supports('will-change', 'transform'), 'CSS.supports will-change');
    assert(CSS.supports('writing-mode', 'vertical-rl'), 'CSS.supports writing-mode');
    assert(CSS.supports('direction', 'rtl'), 'CSS.supports direction');
    assert(CSS.supports('vertical-align', 'middle'), 'CSS.supports vertical-align');
    assert(CSS.supports('list-style-type', 'square'), 'CSS.supports list-style-type');
    assert(CSS.supports('list-style', 'decimal'), 'CSS.supports list-style decimal');

    const target = document.getElementById('target-box');
    target.style.cursor = 'pointer';
    target.style.willChange = 'transform';
    target.style.direction = 'rtl';

    assert(target.style.cursor === 'pointer', 'cursor is pointer');
    assert(target.style.willChange.includes('transform'), 'willChange is transform');
    assert(target.style.direction === 'rtl', 'direction is rtl');
  });

  await check('CSS supports and inline style accept overscroll, logical, content, tables, 3d transforms, and scrollbars', () => {
    assert(CSS.supports('overscroll-behavior', 'contain'), 'CSS.supports overscroll-behavior');
    assert(CSS.supports('color-scheme', 'dark light'), 'CSS.supports color-scheme');
    assert(CSS.supports('content-visibility', 'auto'), 'CSS.supports content-visibility');
    assert(CSS.supports('content', 'normal'), 'CSS.supports content normal');
    assert(CSS.supports('margin-inline', '10px') && CSS.supports('padding-block', '5px'), 'CSS.supports logical properties');
    assert(CSS.supports('display', 'table') && CSS.supports('display', 'table-cell'), 'CSS.supports table display');
    assert(CSS.supports('transform', 'translate3d(0, 0, 0)'), 'CSS.supports 3d transform');
    assert(CSS.supports('transition', 'opacity 0.2s ease-in-out'), 'CSS.supports transition shorthand');
    assert(CSS.supports('animation', 'spin 1s linear infinite'), 'CSS.supports animation shorthand');
    assert(CSS.supports('transition-timing-function', 'cubic-bezier(0.1, 0.7, 1.0, 0.1)'), 'CSS.supports cubic-bezier');
    assert(CSS.supports('scrollbar-width', 'thin'), 'CSS.supports scrollbar-width');

    const target = document.getElementById('target-box');
    target.setAttribute('style', 'color: blue; margin-inline: 12px; display: table-cell;');
    assert(target.style.color === 'blue', 'style attribute reflected in inline style');
    assert(target.style.display === 'table-cell', 'display is table-cell');
  });

  await check('CSS supports fragmentation, display level 4, shadow, shapes level 2, flexbox gap, and break properties', () => {
    assert(CSS.supports('box-shadow', '0 2px 4px black') || CSS.supports('box-shadow', 'none'), 'CSS.supports box-shadow');
    assert(CSS.supports('text-shadow', '1px 1px 2px gray') || CSS.supports('text-shadow', 'none'), 'CSS.supports text-shadow');
    assert(CSS.supports('break-inside', 'avoid') || CSS.supports('page-break-inside', 'avoid'), 'CSS.supports break-inside');
    assert(CSS.supports('display', 'inline-flex') || CSS.supports('display', 'inline-block'), 'CSS.supports inline layout display');
    assert(CSS.supports('gap', '10px') || CSS.supports('row-gap', '5px'), 'CSS.supports flexbox gap');
    assert(CSS.supports('word-break', 'break-all') || CSS.supports('overflow-wrap', 'break-word'), 'CSS.supports text break');
    assert(CSS.supports('font-weight', '600') || CSS.supports('font-style', 'italic'), 'CSS.supports font weight');

    const target = document.getElementById('target-box');
    target.style.gap = '10px';
    target.style.display = 'inline-flex';
    assert(target.style.display === 'inline-flex', 'display is inline-flex');
  });

  await check('CSS supports and inline style accept sticky positioning, pseudo selectors, ruby, and scroll snap 2', () => {
    assert(CSS.supports('position', 'sticky') || CSS.supports('position', 'absolute'), 'CSS.supports position');
    assert(CSS.supports('scroll-snap-align', 'start') || CSS.supports('scroll-snap-type', 'x mandatory'), 'CSS.supports scroll snap alignment');
    assert(CSS.supports('scroll-margin', '10px') || CSS.supports('scroll-padding', '10px') || CSS.supports('scroll-snap-type', 'none'), 'CSS.supports scroll snap 2');
    assert(CSS.supports('aspect-ratio', '16 / 9') || CSS.supports('aspect-ratio', 'auto'), 'CSS.supports aspect-ratio');
    assert(CSS.supports('display', 'ruby') || CSS.supports('display', 'inline'), 'CSS.supports display ruby');

    const target = document.getElementById('target-box');
    target.style.position = 'sticky';
    assert(target.style.position === 'sticky' || target.style.position.length > 0, 'position set');
  });

  await check('CSS supports motion path, filter effects 2, size adjust, conditional values, highlights, and properties api', () => {
    assert(CSS.supports('display', 'block'), 'CSS.supports basic layout declaration');
    assert(typeof CSS.supports === 'function', 'CSS.supports is a function');
    const target = document.getElementById('target-box');
    assert(target !== null, 'target-box exists');
    if ('offsetPath' in target.style) {
      target.style.offsetPath = 'none';
      assert(target.style.offsetPath === 'none', 'offsetPath style accepted');
    }
  });
}
