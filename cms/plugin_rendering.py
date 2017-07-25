# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from collections import deque

from functools import partial

from classytags.utils import flatten_context

from django.contrib.sites.models import Site
from django.template import Context
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe

from cms.cache.placeholder import get_placeholder_cache, set_placeholder_cache
from cms.exceptions import PlaceholderNotFound
from cms.toolbar.utils import (
    get_placeholder_toolbar_js,
    get_plugin_toolbar_js,
    get_toolbar_from_request,
)
from cms.utils import get_language_from_request
from cms.utils.conf import get_cms_setting
from cms.utils.django_load import iterload_objects
from cms.utils.permissions import has_plugin_permission
from cms.utils.placeholder import get_toolbar_plugin_struct, restore_sekizai_context
from cms.utils.plugins import get_plugin_restrictions


def _get_page_ancestors(page):
    """
    Returns a generator which yields the ancestors for page.
    """
    if not page.parent_id:
        raise StopIteration

    # This is done to fetch one parent at a time vs using the tree
    # to get all descendants.
    # The parents have already been loaded by the placeholder pre-loading.
    yield page.parent

    for ancestor in _get_page_ancestors(page.parent):
        yield ancestor


def _unpack_plugins(parent_plugin):
    found_plugins = []

    for plugin in parent_plugin.child_plugin_instances or []:
        found_plugins.append(plugin)

        if plugin.child_plugin_instances:
            found_plugins.extend(_unpack_plugins(plugin))
    return found_plugins


class RenderedPlaceholder(object):
    __slots__ = ('placeholder', 'language', 'site_id', 'cached', 'editable')

    def __init__(self, placeholder, language, site_id, cached=False, editable=False):
        self.placeholder = placeholder
        self.language = language
        self.site_id = site_id
        self.cached = cached
        self.editable = editable

    def __eq__(self, other):
        # The same placeholder rendered with different
        # parameters is considered the same.
        # This behavior is compatible with previous djangoCMS releases.
        return self.placeholder == other.placeholder

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.placeholder)


class BaseRenderer(object):

    placeholder_edit_template = ''

    def __init__(self, request):
        self.request = request
        self.request_language = get_language_from_request(self.request)
        self._cached_templates = {}
        self._cached_plugin_classes = {}
        self._placeholders_content_cache = {}
        self._placeholders_by_page_cache = {}
        self._rendered_placeholders = deque()
        self._rendered_static_placeholders = deque()
        self._rendered_plugins_by_placeholder = {}

    @cached_property
    def current_page(self):
        return self.request.current_page

    @cached_property
    def toolbar(self):
        return get_toolbar_from_request(self.request)

    @cached_property
    def templates(self):
        return self.toolbar.templates

    @cached_property
    def plugin_pool(self):
        import cms.plugin_pool
        return cms.plugin_pool.plugin_pool

    @cached_property
    def site_id(self):
        site = Site.objects.get_current(self.request)
        return site.pk

    def get_placeholder_plugin_menu(self, placeholder, page=None):
        registered_plugins = self.plugin_pool.registered_plugins
        can_add_plugin = partial(has_plugin_permission, user=self.request.user, permission_type='add')
        plugins = [plugin for plugin in registered_plugins if can_add_plugin(plugin_type=plugin.value)]
        plugin_menu = get_toolbar_plugin_struct(
            plugins=plugins,
            slot=placeholder.slot,
            page=page,
        )
        plugin_menu_template = self.templates.placeholder_plugin_menu_template
        return plugin_menu_template.render({'plugin_menu': plugin_menu})

    def get_placeholder_toolbar_js(self, placeholder, language, page=None):
        plugins = self.plugin_pool.get_all_plugins(placeholder.slot, page) # original

        plugin_types = [cls.__name__ for cls in plugins]
        allowed_plugins = plugin_types + self.plugin_pool.get_system_plugins()
        placeholder_toolbar_js = get_placeholder_toolbar_js(
            placeholder=placeholder,
            request_language=self.request_language,
            render_language=language,
            allowed_plugins=allowed_plugins,
        )
        return placeholder_toolbar_js

    def get_plugin_toolbar_js(self, plugin, page=None):
        placeholder_cache = self._rendered_plugins_by_placeholder.setdefault(plugin.placeholder_id, {})
        child_classes, parent_classes = get_plugin_restrictions(
            plugin=plugin,
            page=page,
            restrictions_cache=placeholder_cache,
        )
        content = get_plugin_toolbar_js(
            plugin,
            children=child_classes,
            parents=parent_classes,
            request_language=self.request_language,
        )
        return content

    def get_plugin_class(self, plugin):
        plugin_type = plugin.plugin_type

        if not plugin_type in self._cached_plugin_classes:
            self._cached_plugin_classes[plugin_type] = self.plugin_pool.get_plugin(plugin_type)
        return self._cached_plugin_classes[plugin_type]

    def get_plugins_to_render(self, placeholder, language, template):
        from cms.utils.plugins import get_plugins

        plugins = get_plugins(
            request=self.request,
            placeholder=placeholder,
            template=template,
            lang=language,
        )
        return plugins

    def get_rendered_plugins_cache(self, placeholder):
        blank = {
            'plugins': [],
            'plugin_parents': {},
            'plugin_children': {},
        }
        return self._rendered_plugins_by_placeholder.get(placeholder.pk, blank)

    def get_rendered_placeholders(self):
        return [r.placeholder for r in self._rendered_placeholders]

    def get_rendered_editable_placeholders(self):
        return [r.placeholder for r in self._rendered_placeholders if r.editable]

    def get_rendered_static_placeholders(self):
        return self._rendered_static_placeholders


class ContentRenderer(BaseRenderer):

    plugin_edit_template = (
        '<template class="cms-plugin '
        'cms-plugin-start cms-plugin-{pk}"></template>{content}'
        '<template class="cms-plugin cms-plugin-end cms-plugin-{pk}"></template>'
    )
    placeholder_edit_template = (
        '{content} '
        '<div class="cms-placeholder cms-placeholder-{placeholder_id}"></div> '
        '<script data-cms>{plugin_js}\n{placeholder_js}</script>'
    )

    def __init__(self, request):
        super(ContentRenderer, self).__init__(request)
        self._placeholders_are_editable = bool(self.toolbar.edit_mode_active)

    def placeholder_cache_is_enabled(self):
        if not get_cms_setting('PLACEHOLDER_CACHE'):
            return False
        if self.request.user.is_staff:
            return False
        return not self._placeholders_are_editable

    def render_placeholder(self, placeholder, context, language=None, page=None,
                           editable=False, use_cache=False, nodelist=None, width=None):
        from sekizai.helpers import Watcher

        language = language or self.request_language
        editable = editable and self._placeholders_are_editable

        if use_cache and not editable and placeholder.cache_placeholder:
            use_cache = self.placeholder_cache_is_enabled()
        else:
            use_cache = False

        if use_cache:
            cached_value = self._get_cached_placeholder_content(
                placeholder=placeholder,
                language=language,
            )
        else:
            cached_value = None

        if cached_value is not None:
            # User has opted to use the cache
            # and there is something in the cache
            restore_sekizai_context(context, cached_value['sekizai'])
            return mark_safe(cached_value['content'])

        context.push()

        width = width or placeholder.default_width
        template = page.get_template() if page else None

        if width:
            context['width'] = width

        # Add extra context as defined in settings, but do not overwrite existing context variables,
        # since settings are general and database/template are specific
        # TODO this should actually happen as a plugin context processor, but these currently overwrite
        # existing context -- maybe change this order?
        for key, value in placeholder.get_extra_context(template).items():
            if key not in context:
                context[key] = value

        if use_cache:
            watcher = Watcher(context)

        plugin_content = self.render_plugins(
            placeholder,
            language=language,
            context=context,
            editable=editable,
            template=template,
        )
        placeholder_content = ''.join(plugin_content)

        if not placeholder_content and nodelist:
            # should be nodelist from a template
            placeholder_content = nodelist.render(context)

        if use_cache:
            content = {
                'content': placeholder_content,
                'sekizai': watcher.get_changes(),
            }
            set_placeholder_cache(
                placeholder,
                lang=language,
                site_id=self.site_id,
                content=content,
                request=self.request,
            )

        rendered_placeholder = RenderedPlaceholder(
            placeholder=placeholder,
            language=language,
            site_id=self.site_id,
            cached=use_cache,
            editable=editable,
        )

        if rendered_placeholder not in self._rendered_placeholders:
            # First time this placeholder is rendered
            if not self.toolbar._cache_disabled:
                # The toolbar middleware needs to know if the response
                # is to be cached.
                # Set the _cache_disabled flag to the value of cache_placeholder
                # only if the flag is False (meaning cache is enabled).
                self.toolbar._cache_disabled = not use_cache
            self._rendered_placeholders.append(rendered_placeholder)

        if editable:
            data = self.get_editable_placeholder_context(placeholder, language, page=page)
            data['content'] = placeholder_content
            placeholder_content = self.placeholder_edit_template.format(**data)

        context.pop()
        return mark_safe(placeholder_content)

    def get_editable_placeholder_context(self, placeholder, language, page=None):
        placeholder_cache = self.get_rendered_plugins_cache(placeholder)
        placeholder_toolbar_js = self.get_placeholder_toolbar_js(
            placeholder,
            language=language,
            page=page,
        )
        plugin_toolbar_js_bits = (self.get_plugin_toolbar_js(plugin, page=page)
                                  for plugin in placeholder_cache['plugins'])
        context = {
            'plugin_js': ''.join(plugin_toolbar_js_bits),
            'placeholder_js': placeholder_toolbar_js,
            'placeholder_id': placeholder.pk,
        }
        return context

    def render_page_placeholder(self, slot, context, inherit=False, nodelist=None):
        current_page = self.request.current_page

        if not current_page:
            return ''

        content = self._render_page_placeholder(
            context=context,
            slot=slot,
            page=current_page,
            editable=True,
            nodelist=nodelist,
        )

        if content or not current_page.parent_id:
            return content

        # don't display inherited plugins in edit mode, so that the user doesn't
        # mistakenly edit/delete them. This is a fix for issue #1303. See the discussion
        # there for possible enhancements
        if not inherit or self.toolbar.edit_mode_active:
            return content

        if current_page.parent_id not in self._placeholders_by_page_cache:
            # The placeholder cache is primed when the first placeholder
            # is loaded. If the current page's parent is not in there,
            # it means its cache was never primed as it wasn't necessary.
            return content

        cache_enabled = self.placeholder_cache_is_enabled()

        for page in _get_page_ancestors(current_page):
            page_placeholders = self._placeholders_by_page_cache[page.pk]

            try:
                placeholder = page_placeholders[slot]
            except KeyError:
                continue

            try:
                has_prefetched_plugins = bool(placeholder._plugins_cache)
            except AttributeError:
                has_prefetched_plugins = False

            if cache_enabled and not has_prefetched_plugins:
                cached_content = self._get_cached_placeholder_content(
                    placeholder=placeholder,
                    language=self.request_language,
                )
                has_cached_content = cached_content is not None
            else:
                has_cached_content = False

            if has_prefetched_plugins or has_cached_content:
                # nodelist is set to None to avoid rendering the nodes inside
                # a {% placeholder or %} block tag.
                # When placeholder inheritance is used, we only care about placeholders
                # with plugins.
                inherited_content = self.render_placeholder(
                    placeholder,
                    context=context,
                    page=page,
                    editable=False,
                    use_cache=True,
                    nodelist=None,
                )
                return inherited_content
        return content

    def render_static_placeholder(self, static_placeholder, context, nodelist=None):
        user = self.request.user

        if self.toolbar.edit_mode_active and user.has_perm('cms.edit_static_placeholder'):
            placeholder = static_placeholder.draft
            editable = True
            use_cache = False
        else:
            placeholder = static_placeholder.public
            editable = False
            use_cache = True

        # I really don't like these impromptu flags...
        placeholder.is_static = True

        content = self.render_placeholder(
            placeholder,
            context=context,
            editable=editable,
            use_cache=use_cache,
            nodelist=nodelist,
        )

        if static_placeholder not in self._rendered_static_placeholders:
            # First time this static placeholder is rendered
            self._rendered_static_placeholders.append(static_placeholder)
        return content

    def render_plugin(self, instance, context, placeholder=None, editable=False):
        if not placeholder:
            placeholder = instance.placeholder

        instance, plugin = instance.get_plugin_instance()

        if not instance or not plugin.render_plugin:
            return ''

        # we'd better pass a flat dict to template.render
        # as plugin.render can return pretty much any kind of context / dictionary
        # we'd better flatten it and force to a Context object
        # flattening the context means that template must be an engine-specific template object
        # which is guaranteed by get_cached_template if the template returned by
        # plugin._get_render_template is either a string or an engine-specific template object
        context = PluginContext(context, instance, placeholder)
        context = plugin.render(context, instance, placeholder.slot)
        context = flatten_context(context)

        template = plugin._get_render_template(context, instance, placeholder)
        template = self.templates.get_cached_template(template)

        content = template.render(context)

        for processor in iterload_objects(get_cms_setting('PLUGIN_PROCESSORS')):
            content = processor(instance, placeholder, content, context)

        if editable:
            content = self.plugin_edit_template.format(pk=instance.pk, content=content)
            placeholder_cache = self._rendered_plugins_by_placeholder.setdefault(placeholder.pk, {})
            placeholder_cache.setdefault('plugins', []).append(instance)
        return mark_safe(content)

    def render_plugins(self, placeholder, language, context, editable=False, template=None):
        plugins = self.get_plugins_to_render(
            placeholder=placeholder,
            template=template,
            language=language,
        )

        for plugin in plugins:
            plugin._placeholder_cache = placeholder
            yield self.render_plugin(plugin, context, placeholder, editable)

    def _get_cached_placeholder_content(self, placeholder, language):
        """
        Returns a dictionary mapping placeholder content and sekizai data.
        Returns None if no cache is present.
        """
        # Placeholders can be rendered multiple times under different sites
        # it's important to have a per-site "cache".
        site_cache = self._placeholders_content_cache.setdefault(self.site_id, {})
        # Placeholders can be rendered multiple times under different languages
        # it's important to have a per-language "cache".
        language_cache = site_cache.setdefault(language, {})

        if placeholder.pk not in language_cache:
            cached_value = get_placeholder_cache(
                placeholder,
                lang=language,
                site_id=self.site_id,
                request=self.request,
            )

            if cached_value != None:
                # None means nothing in the cache
                # Anything else is a valid value
                language_cache[placeholder.pk] = cached_value
        return language_cache.get(placeholder.pk)

    def _get_page_placeholder(self, context, page, slot):
        """
        Returns a Placeholder instance attached to page that
        matches the given slot.

        A PlaceholderNotFound is raised if the placeholder is
        not present on the page template.
        """
        placeholder_cache = self._placeholders_by_page_cache

        if page.pk not in placeholder_cache:
            # Instead of loading plugins for this one placeholder
            # try and load them for all placeholders on the page.
            self._preload_placeholders_for_page(page)

        try:
            placeholder = placeholder_cache[page.pk][slot]
        except KeyError:
            message = '"%s" placeholder not found' % slot
            raise PlaceholderNotFound(message)
        return placeholder

    def _render_page_placeholder(self, context, slot, page, editable=True, nodelist=None):
        """
        Renders a placeholder attached to a page.
        """
        placeholder = self._get_page_placeholder(context, page, slot)

        content = self.render_placeholder(
            placeholder,
            context=context,
            page=page,
            editable=editable,
            use_cache=True,
            nodelist=nodelist,
        )
        return content

    def _preload_placeholders_for_page(self, page, slots=None, inherit=False):
        """
        Populates the internal plugin cache of each placeholder
        in the given page if the placeholder has not been
        previously cached.
        """
        from cms.utils.plugins import assign_plugins

        if slots:
            placeholders = page.get_placeholders().filter(slot__in=slots)
        else:
            # Creates any placeholders missing on the page
            placeholders = page.rescan_placeholders().values()

        if inherit:
            # When the inherit flag is True,
            # assume all placeholders found are inherited and thus prefetch them.
            slots_w_inheritance = [pl.slot for pl in placeholders]
        elif not self.toolbar.edit_mode_active:
            # Scan through the page template to find all placeholders
            # that have inheritance turned on.
            slots_w_inheritance = [pl.slot for pl in page.get_declared_placeholders() if pl.inherit]
        else:
            # Inheritance is turned off on edit-mode
            slots_w_inheritance = []

        if self.placeholder_cache_is_enabled():
            _cached_content = self._get_cached_placeholder_content
            # Only prefetch plugins if the placeholder
            # has not been cached.
            placeholders_to_fetch = [
                placeholder for placeholder in placeholders
                if _cached_content(placeholder, self.request_language) == None]
        else:
            # cache is disabled, prefetch plugins for all
            # placeholders in the page.
            placeholders_to_fetch = placeholders

        if placeholders_to_fetch:
            assign_plugins(
                request=self.request,
                placeholders=placeholders_to_fetch,
                template=page.get_template(),
                lang=self.request_language,
                is_fallback=inherit,
            )

        # Inherit only placeholders that have no plugins
        # or are not cached.
        placeholders_to_inherit = [
            pl.slot for pl in placeholders
            if not getattr(pl, '_plugins_cache', None) and pl.slot in slots_w_inheritance
        ]

        if placeholders_to_inherit and page.parent_id:
            self._preload_placeholders_for_page(
                page=page.parent,
                slots=placeholders_to_inherit,
                inherit=True,
            )

        # Internal cache mapping placeholder slots
        # to placeholder instances.
        page_placeholder_cache = {}

        for placeholder in placeholders:
            # Save a query when the placeholder toolbar is rendered.
            placeholder.page = page
            page_placeholder_cache[placeholder.slot] = placeholder

        self._placeholders_by_page_cache[page.pk] = page_placeholder_cache


class StructureRenderer(BaseRenderer):

    placeholder_edit_template = (
        """
        <script data-cms id="cms-plugin-child-classes-{placeholder_id}" type="text/cms-template">
            {plugin_menu_js}
        </script>
        <script data-cms>{plugin_js}\n{placeholder_js}</script>
        """
    )

    def get_plugins_to_render(self, *args, **kwargs):
        plugins = super(StructureRenderer, self).get_plugins_to_render(*args, **kwargs)

        for plugin in plugins:
            yield plugin

            if not plugin.child_plugin_instances:
                continue

            for plugin in _unpack_plugins(plugin):
                yield plugin

    def render_placeholder(self, placeholder, language, page=None):
        rendered_plugins = self.render_plugins(placeholder, language=language, page=page)
        plugin_js_output = ''.join(rendered_plugins)

        placeholder_toolbar_js = self.get_placeholder_toolbar_js(
            placeholder,
            language=language,
            page=page,
        )

        rendered_placeholder = RenderedPlaceholder(
            placeholder=placeholder,
            language=language,
            site_id=self.site_id,
            cached=False,
            editable=True,
        )

        if rendered_placeholder not in self._rendered_placeholders:
            self._rendered_placeholders.append(rendered_placeholder)

        placeholder_structure_is = self.placeholder_edit_template.format(
            placeholder_id=placeholder.pk,
            plugin_js=plugin_js_output,
            plugin_menu_js=self.get_placeholder_plugin_menu(placeholder, page=page),
            placeholder_js=placeholder_toolbar_js,
        )
        return mark_safe(placeholder_structure_is)

    def render_page_placeholder(self, page, placeholder, language=None):
        return self.render_placeholder(placeholder, language=language, page=page)

    def render_static_placeholder(self, static_placeholder, language=None):
        user = self.request.user

        if not user.has_perm('cms.edit_static_placeholder'):
            return ''

        language = language or self.request_language

        placeholder = static_placeholder.draft
        # I really don't like these impromptu flags...
        placeholder.is_static = True

        content = self.render_placeholder(placeholder, language=language)

        if static_placeholder not in self._rendered_static_placeholders:
            # First time this static placeholder is rendered
            self._rendered_static_placeholders.append(static_placeholder)
        return content

    def render_plugin(self, instance, page=None):
        placeholder_cache = self._rendered_plugins_by_placeholder.setdefault(instance.placeholder_id, {})
        placeholder_cache.setdefault('plugins', []).append(instance)
        return self.get_plugin_toolbar_js(instance, page=page)

    def render_plugins(self, placeholder, language, page=None):
        template = page.get_template() if page else None
        plugins = self.get_plugins_to_render(placeholder, language, template)

        for plugin in plugins:
            plugin._placeholder_cache = placeholder
            yield self.render_plugin(plugin, page=page)


class LegacyRenderer(ContentRenderer):

    placeholder_edit_template = (
        """
        {content}
        <div class="cms-placeholder cms-placeholder-{placeholder_id}"></div>
        <script data-cms id="cms-plugin-child-classes-{placeholder_id}" type="text/cms-template">
            {plugin_menu_js}
        </script>
        <script data-cms>{plugin_js}\n{placeholder_js}</script>
        """
    )

    def get_editable_placeholder_context(self, placeholder, language, page=None):
        context = super(LegacyRenderer, self).get_editable_placeholder_context(
            placeholder=placeholder,
            language=language,
            page=page,
        )
        context['plugin_menu_js'] = self.get_placeholder_plugin_menu(placeholder, page=page)
        return context


class PluginContext(Context):
    """
    This subclass of template.Context automatically populates itself using
    the processors defined in CMS_PLUGIN_CONTEXT_PROCESSORS.
    Additional processors can be specified as a list of callables
    using the "processors" keyword argument.
    """

    def __init__(self, dict_, instance, placeholder, processors=None, current_app=None):
        dict_ = flatten_context(dict_)
        super(PluginContext, self).__init__(dict_)

        if not processors:
            processors = []

        for processor in iterload_objects(get_cms_setting('PLUGIN_CONTEXT_PROCESSORS')):
            self.update(processor(instance, placeholder, self))
        for processor in processors:
            self.update(processor(instance, placeholder, self))
