
---
name: guardia-lambda
description: Guía para implementar AWS Lambda siguiendo la arquitectura, seguridad y estándares del proyecto Guardia.
---

# Skill: Guardia Lambda

## Objetivo

Establecer el procedimiento para crear o modificar funciones AWS Lambda de forma consistente con la arquitectura y los requisitos del proyecto Guardia.

## Antes de comenzar

1. Consultar `tasks.md` para identificar la tarea que se va a implementar.
2. Consultar `requirements.md` para identificar los requisitos relacionados.
3. Revisar `tech.md` para conocer las reglas técnicas y de seguridad.
4. Revisar el código existente para reutilizar componentes y convenciones del proyecto.

## Arquitectura

1. Mantener el handler de Lambda delgado.
2. Delegar la lógica de negocio reutilizable en `src/guardia_common/`.
3. Ubicar el código específico de cada función en la estructura de `src/lambdas/` definida por el proyecto.
4. Reutilizar los modelos Pydantic existentes para validar datos de entrada y salida cuando corresponda.

## Auditoría y errores

1. Aplicar el decorador de auditoría establecido por el proyecto.
2. Manejar errores de forma controlada y consistente.
3. Evitar registrar credenciales, tokens u otra información sensible.
4. Respetar el catálogo de errores y las convenciones documentadas.

## Seguridad

1. Aplicar el principio de mínimo privilegio a los permisos IAM de cada Lambda.
2. Evitar permisos amplios que no sean necesarios para la función.
3. Obtener los umbrales y parámetros operativos desde AWS Systems Manager Parameter Store (SSM), según la configuración del proyecto.
4. No incluir secretos ni credenciales directamente en el código.

## Pruebas y validación

1. Crear pruebas unitarias para la lógica implementada.
2. Asociar las pruebas con los IDs reales de requisitos mediante el marcador `@pytest.mark.req("x.y")`.
3. Cubrir los casos exitosos, los errores y los límites relevantes.
4. Ejecutar las pruebas y los controles de calidad configurados en el repositorio.
5. Corregir los fallos antes de solicitar la revisión del cambio.

## Restricciones

- No inventar requisitos, nombres de recursos ni parámetros de configuración.
- No duplicar lógica compartida que corresponda a `guardia_common`.
- No agregar permisos IAM innecesarios.
- No considerar terminada una tarea sin validar los requisitos y las pruebas aplicables.

## Criterios de finalización

La implementación debe cumplir los requisitos documentados, respetar la arquitectura y las reglas de seguridad, incluir pruebas pertinentes y superar los controles de calidad establecidos por el proyecto.
