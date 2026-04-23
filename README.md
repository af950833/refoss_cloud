# Refoss Cloud for Home Assistant

Refoss EM06을 Home Assistant에서 사용하기 위한 비공식 커스텀 통합입니다.

이 통합은 Refoss 클라우드 HTTP API와 클라우드 MQTT를 이용해 EM06의 채널별 전력 정보를 가져옵니다.  
로컬 `/public` API를 사용하지 않으며, 다음 값을 Home Assistant 센서로 제공합니다.

- 검침일 기준 월 사용량
- 오늘 사용량
- 현재 전력
- 전압
- 역률
- 전류

태양광 패널이 연결된 채널처럼 순사용량이 음수가 될 수 있는 환경도 그대로 처리합니다.

## 주요 기능

- Home Assistant GUI 설정 지원
- Refoss 계정 로그인 후 EM06 자동 검색
- 채널별 센서 생성
  - A1
  - B1
  - C1
  - A2
  - B2
  - C2
- 검침일 선택
  - 1일 ~ 27일
  - 말일
- MQTT 조회 주기 설정
  - 기본 15초
  - 최소 10초
- 채널별 센서 제공
  - Billing month energy
  - This Day Energy
  - Power
  - Voltage
  - PF
  - Current
- 태양광 채널의 음수 순사용량 유지

## 지원 대상

- Refoss EM06
- Home Assistant 커스텀 통합

현재 구현은 EM06을 기준으로 작성되었습니다.

## 데이터 조회 방식

이 통합은 두 가지 클라우드 경로를 사용합니다.

### 1. Cloud MQTT

다음 값은 Refoss 클라우드 MQTT를 통해 조회합니다.

- `Appliance.Control.ElectricityX`
  - `mConsume`
  - `power`
  - `voltage`
  - `factor`
  - `current`
- `Appliance.Control.ConsumptionH`
  - 채널별 오늘 사용량
  - 최근 시간대별 사용량 일부
  - HTTP 1시간 이력에 누락이 있을 때 보정용으로 사용

`ElectricityX`는 한 번의 요청으로 6채널 전체를 가져옵니다.  
`ConsumptionH`는 채널별로 조회합니다.

### 2. Cloud HTTP History

과거 사용량은 Refoss 클라우드 HTTP history API를 사용합니다.

```text
/historage/v1/deviceTelemetry/query
```

조회 조건:

```text
metric: electricH
queryType: stepSum
step: 1d 또는 1h
```

## 계산 방식

## Billing month energy

`Billing month energy`는 검침일 기준 월 사용량입니다.

현재 로직은 다음과 같습니다.

```text
검침 시작일 ~ 2일 전   -> HTTP history 1d
어제                  -> HTTP history 1h
어제 누락 시간대       -> ConsumptionH로 보정
오늘                  -> 현재 mConsume - 오늘 00:00 스냅샷
```

즉 최종 계산은 다음과 같습니다.

```text
Billing month energy =
  어제까지의 누적 사용량
  + 오늘 사용량
```

검침일 당일 00:00이 지나면 새 검침월이 시작되므로:

```text
Billing month energy == This Day Energy
```

가 되는 것이 정상입니다.

## This Day Energy

`This Day Energy`는 오늘 00:00 이후의 순사용량입니다.

```text
This Day Energy =
  현재 mConsume - 오늘 00:00 스냅샷
```

태양광 채널처럼 순사용량이 음수가 될 수 있는 경우도 그대로 음수로 표시합니다.

## 스냅샷 동작

오늘 사용량 계산을 위해 채널별 `mConsume` 기준값을 저장합니다.

- 저장 시각: 매일 00:00:00
- 저장 위치: `.storage/refoss_cloud_mconsume_snapshots`
- 보관 기간: 최근 40일

정확한 자정 스냅샷이 있으면 그것을 우선 사용합니다.

최초 설치, 재시작, 비정상 종료 등으로 오늘 스냅샷이 없으면 현재 `mConsume`과 오늘 `ConsumptionH`를 이용해 임시 기준값을 복원할 수 있습니다.

## 월초 리셋 보호

EM06의 `mConsume`은 월초에 리셋될 수 있으므로, 다음 구간에서는 보호 로직이 동작합니다.

- 말일 23:50 ~ 1일 00:10

동작 방식:

- 이 구간에서는 일반 backfill을 제한
- 1일 00:00 ~ 00:10 사이 `mConsume`이 `0 ~ 50 Wh`로 들어온 채널만 0 스냅샷으로 처리

이 로직은 월초 리셋 타이밍이 채널마다 조금 다를 때 값이 튀는 문제를 줄이기 위한 것입니다.

## HTTP history 캐시

HTTP history는 매 폴링마다 호출하지 않습니다.

기본 갱신 시점:

- Home Assistant 시작 후 첫 갱신
- 매일 00:00:05 이후 첫 갱신
- 매일 00:05:00 이후 첫 갱신

이후 같은 캐시 구간에서는 MQTT 값만 갱신하고, 과거 HTTP history는 재사용합니다.

즉 일반적인 동작에서는:

- `ElectricityX`는 설정한 MQTT 주기마다 조회
- `ConsumptionH`는 history 캐시 갱신이 필요한 시점에만 조회
- HTTP history는 하루 중 제한된 시점에만 조회

## 채널 매핑

EM06 원본 채널 번호는 다음과 같이 표시됩니다.

```text
1 -> A1
2 -> B1
3 -> C1
4 -> A2
5 -> B2
6 -> C2
```

## 생성되는 센서

예: `A1` 채널

```text
Refoss EM06 A1 Billing month energy
Refoss EM06 A1 This Day Energy
Refoss EM06 A1 Power
Refoss EM06 A1 Voltage
Refoss EM06 A1 PF
Refoss EM06 A1 Current
```

## 단위 변환

Refoss 원본 응답값은 아래와 같이 Home Assistant 표시 단위로 변환됩니다.

```text
mConsume / 1000 -> kWh
power    / 1000 -> W
voltage  / 1000 -> V
current  / 1000 -> A
factor          -> PF
```

표시 정밀도는 소수점 이하 3자리입니다.

## 설치 방법

### HACS 설치

1. Home Assistant에서 HACS를 엽니다.
2. 우측 상단 메뉴에서 `Custom repositories`를 선택합니다.
3. 아래 주소를 추가합니다.

```text
https://github.com/af950833/refoss_cloud
```

4. Category는 `Integration`을 선택합니다.
5. `Refoss Cloud`를 설치합니다.
6. Home Assistant를 재시작합니다.

### 수동 설치

다음 폴더를 Home Assistant의 `custom_components/refoss_cloud` 아래에 복사합니다.

```text
custom_components/refoss_cloud/
  __init__.py
  config_flow.py
  manifest.json
  sensor.py
  strings.json
```

복사 후 Home Assistant를 재시작합니다.

## 설정 방법

1. Home Assistant에서 `설정` > `기기 및 서비스`로 이동합니다.
2. `통합 구성요소 추가`를 선택합니다.
3. `Refoss Cloud`를 검색합니다.
4. Refoss 계정 이메일과 비밀번호를 입력합니다.
5. 계정에 연결된 EM06을 선택합니다.
6. 아래 항목을 설정합니다.
   - 이름
   - 검침일
   - 채널
   - MQTT 업데이트 주기

## 옵션

설치 후 옵션 화면에서 MQTT 업데이트 주기를 변경할 수 있습니다.

- 기본: 15초
- 최소: 10초

옵션을 저장하면 통합이 다시 로드됩니다.

## 주요 속성

`Billing month energy`와 `This Day Energy`에는 계산 확인용 속성이 일부 포함됩니다.

주요 예시:

- `current_mconsume_kwh`
- `today_snapshot_kwh`
- `today_snapshot_delta_kwh`
- `completed_history_kwh`
- `yesterday_filled_hours`
- `yesterday_missing_hours`

특히 아래 값이 유용합니다.

- `today_snapshot_kwh`
  - 오늘 사용량 계산의 기준이 되는 00:00 스냅샷
- `completed_history_kwh`
  - 검침 시작일부터 어제까지의 누적 사용량
- `yesterday_missing_hours`
  - 어제 1시간 history에서 끝까지 채우지 못한 시간대

## 에너지 대시보드

에너지 센서는 아래 속성을 사용합니다.

```text
device_class: energy
state_class: total
unit_of_measurement: kWh
last_reset: 자동 계산
```

`Billing month energy`는 현재 검침월 시작 시각을 `last_reset`으로 사용합니다.  
`This Day Energy`는 오늘 00:00을 `last_reset`으로 사용합니다.

## 문제 확인 포인트

값이 이상할 때는 아래를 먼저 확인해 보세요.

### 오늘 사용량이 이상한 경우

- `today_snapshot_kwh`
- `today_snapshot_delta_kwh`
- 현재 시각이 자정 직후인지

### 월 사용량이 이상한 경우

- `period_start`
- `period_end`
- `completed_history_kwh`
- `yesterday_missing_hours`

### 자정 직후 값이 튀는 경우

다음을 확인해 보세요.

- 자정 스냅샷이 정상 생성되었는지
- `.storage/refoss_cloud_mconsume_snapshots`의 새 날짜 항목이 있는지
- 월초(1일) 리셋 보호 구간에 해당하는지

## 참고 사항

- 이 통합은 비공식 통합입니다.
- Refoss가 공식 문서로 공개한 API가 아니므로, 서버 응답 형식이 바뀌면 수정이 필요할 수 있습니다.
- 현재 구현은 EM06 기준입니다.
- 로컬 EM06 `/public` API는 사용하지 않습니다.
- 오직 Refoss 클라우드 HTTP API와 클라우드 MQTT만 사용합니다.

## 면책

이 프로젝트는 Refoss의 공식 제품이 아니며, Refoss와 직접적인 관련이 없습니다.  
사용 중 발생하는 문제에 대해 공식 지원을 제공하지 않습니다.
