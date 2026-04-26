# TODO - Implementar lectura manual por fecha y recálculo de estadísticas

## Objetivo
Permitir ejecutar consultas manuales desde Home Assistant para recuperar consumo de un día concreto, y que esos datos se incorporen al historial con recálculo correcto de estadísticas.

## Contexto y motivación
Actualmente la integración hace fetch automático a una hora fija (12:30). Si una ejecución falla o la API de i-DE no devuelve datos a tiempo, se necesita un mecanismo manual para recuperar días atrasados.

La nueva funcionalidad debe permitir:
- Lanzar una consulta bajo demanda para una fecha concreta.
- Confirmar resultado en notificaciones persistentes de Home Assistant.
- Incorporar esos datos a estadísticas históricas (backfill) sin romper acumulados ni duplicar registros.

## Enfoque recomendado
### Fase 1 (MVP robusto)
- [x] Crear un servicio manual en el dominio de la integración.
- [x] Aceptar fecha objetivo (YYYY-MM-DD).
- [x] Consultar i-DE para ese día.
- [x] Notificar éxito/error con notificación persistente.
- [x] Guardar trazabilidad de la ejecución manual en storage.

### Fase 2 (funcionalidad completa pedida)
- [x] Añadir backfill de estadísticas en recorder.
- [x] Recalcular acumulados para mantener consistencia temporal.
- [x] Evitar duplicados por fecha/bloque horario.
- [x] Exponer sensores auxiliares de estado de ejecución manual.

## Diseño funcional detallado
### 1) Servicio manual de Home Assistant
Archivo principal: custom_components/ideenergy/__init__.py

Servicio propuesto:
- ideenergy.fetch_day_reading

Parámetros:
- date (requerido): string formato YYYY-MM-DD.
- entry_id (opcional): para seleccionar config entry concreta si hubiera más de una.
- notify (opcional, default true): crear notificación persistente.
- force (opcional, default false): ignorar throttling/caché de seguridad.
- backfill_statistics (opcional, default true): insertar/recalcular estadísticas.

Validaciones:
- Formato de fecha válido.
- Fecha no futura.
- Fecha dentro de un rango razonable (ejemplo: últimos 730 días).

Comportamiento esperado:
- Ejecutable desde Herramientas de desarrollador > Servicios.
- Invocable desde automatizaciones y scripts.

### 2) Lógica de consulta manual en coordinator
Archivo principal: custom_components/ideenergy/coordinator.py

Añadir método nuevo, por ejemplo:
- async_fetch_historical_consumption_for_date(target_date, force=False)

Responsabilidades:
1. Calcular start/end del día objetivo (zona Europe/Madrid).
2. Refrescar sesión si procede (login + get_contract_details).
3. Ejecutar get_historical_consumption(start, end).
4. Transformar periodos en HistoricalState (kWh).
5. Devolver resultado estructurado:
   - date
   - periods
   - total
   - periods_count
   - warnings

Notas:
- Reutilizar lógica existente de parseo y gestión de errores.
- Mantener logs de diagnóstico ya implementados (_async_api_call).

### 3) Notificaciones persistentes al usuario
Archivo principal: custom_components/ideenergy/coordinator.py

Mantener helper de notificación y extender mensajes para modo manual.

Notificación de éxito (manual):
- Título: i-DE: Lectura manual completada
- Contenido:
  - Fecha consultada
  - Periodos recibidos (X/24)
  - Total del día
  - Hora de ejecución
  - Estado del backfill

Notificación de error (manual):
- Título: i-DE: Error en lectura manual
- Contenido:
  - Fecha consultada
  - Motivo del error
  - Hora del error

## Backfill y recálculo de estadísticas (requisito clave)
### Objetivo
Cuando se recupera un día antiguo, no solo mostrar el valor puntual, sino insertar/actualizar los bloques horarios y recalcular el acumulado para no dejar huecos ni inconsistencias.

### Estrategia técnica recomendada
- [x] Normalizar los 24 periodos en bloques horarios UTC equivalentes.
- [x] Leer estadísticas existentes para ese sensor y rango afectado.
- [x] Aplicar merge idempotente:
   - Si existe un bloque, actualizarlo si force=true o si está vacío/inválido.
   - Si no existe, insertarlo.
- [x] Recalcular campo sum desde el primer bloque afectado hasta el final del rango impactado.
- [x] Escribir en recorder/estadísticas respetando metadatos y unidad (kWh).

### Consideraciones importantes
- Evitar doble escritura del mismo bloque horario.
- Asegurar coherencia de timezone (Europe/Madrid en origen, UTC en storage).
- Tratar días con 23 o 25 horas (cambio de hora DST) sin asumir siempre 24.
- Mantener integridad de StatisticMetaData.

### Reglas de idempotencia
- Repetir la misma llamada manual para la misma fecha no debe duplicar datos.
- Solo debe modificar estadísticas cuando haya cambios reales.

## Persistencia de trazabilidad (store)
Archivo principal: custom_components/ideenergy/store.py

Guardar estado mínimo de ejecución manual:
- manual_last_success_time
- manual_last_requested_date
- manual_last_result_summary
- manual_last_backfill_status
- manual_last_error

## Entidades auxiliares recomendadas
Archivo principal: custom_components/ideenergy/sensor.py

Añadir sensores de diagnóstico manual:
- Manual Last Refresh Time (timestamp)
- Manual Last Requested Date (texto)
- Manual Last Result (texto)
- Manual Last Backfill Status (texto)

## Riesgos y mitigaciones
1. Datos no publicados todavía para la fecha solicitada
- Mitigación: mensaje claro sin tratar como crash; notificación informativa.

2. Inconsistencia de acumulados tras backfill parcial
- Mitigación: recalcular sum desde el primer punto afectado.

3. Multiples config entries del dominio
- Mitigación: permitir entry_id en el servicio y resolver de forma explícita.

4. Sobrecarga por muchas reparaciones históricas seguidas
- Mitigación: límite de rango por llamada y colas serializadas por entry.

## Criterios de aceptación
- [x] El servicio ideenergy.fetch_day_reading aparece en HA.
- [x] Una llamada manual válida consulta i-DE para la fecha indicada.
- [x] Se genera notificación persistente de éxito o error.
- [x] Si backfill_statistics=true, las estadísticas del día se insertan/actualizan.
- [x] El acumulado (sum) queda consistente tras backfill.
- [x] Repetir la misma llamada no duplica datos.
- [x] Sensores auxiliares reflejan la última ejecución manual.

## Plan de implementación sugerido
- [x] Implementar contrato del servicio + validación de payload.
- [x] Implementar método de consulta por fecha en coordinator.
- [x] Añadir notificaciones específicas para flujo manual.
- [x] Implementar capa de backfill y recálculo en recorder.
- [x] Añadir persistencia de trazabilidad y sensores auxiliares.
- [x] Añadir pruebas unitarias e integración.

## Plan de pruebas
### Unitarias
- [x] Validación de fecha y parámetros del servicio.
- [x] Transformación de periodos a bloques horarios.
- [x] Idempotencia de merge de estadísticas.
- [x] Recalculo correcto de sum.

### Integración
- [x] Config entry real + servicio manual desde HA test harness.
- [x] Caso éxito con 24 periodos.
- [x] Caso sin datos para fecha.
- [x] Caso error de autenticación/sesión.
- [x] Repetición de llamada sin duplicados.

### E2E (entorno temporal)
- [ ] Levantar HA en contenedor limpio.
- [ ] Ejecutar servicio manual para día histórico.
- [ ] Verificar notificación + estadísticas en UI.

## Nota de despliegue
Para reducir riesgo, activar primero sin backfill automático por defecto (feature flag), validar comportamiento en producción, y luego habilitar backfill por defecto.
