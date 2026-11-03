# 하루 영양 목표 자동 계산 기준

하루 영양 목표(열량·탄수화물·단백질·지방)는 **서버 코드([backend/nutrition.py](backend/nutrition.py))가 고정된 계산식으로** 만들어요. AI는 숫자를 만들지 않아요. AI는 계산 결과를 쉬운 말로 설명하고, 사용자가 적은 메모에 전문가 상담이 필요한 내용(임신, 질환 등)이 있으면 알려주는 일만 해요. 설명에 계산에 없는 숫자가 들어가면 그 문장은 서버가 지워요.

각 값에는 표시를 붙였어요.
- **[근거]**: 공개 자료에서 직접 확인한 값
- **[앱 선택]**: 근거가 제시한 범위 안에서 앱이 고른 대표값
- **[앱 규칙]**: 근거 자료에 없는 앱 자체 규칙

## 필요한 입력

키, 몸무게, 만 나이, 성별(계산식용), 평소 활동량(운동 제외), 해당되는 상황, 목표, 운동 계획(휴식 요일 또는 주당 횟수), 1회 운동 시간이 필요해요. 하나라도 없으면 **추측하지 않고** 빠진 항목을 안내해요. 골격근량은 **제지방량과 다른 값**이라 계산에 쓰지 않아요.

## 계산 순서

1. **안정 시 에너지 (RMR)**: Mifflin-St Jeor 식 **[근거]**
   - 남성: `10 × 몸무게(kg) + 6.25 × 키(cm) − 5 × 나이 + 5`
   - 여성: `10 × 몸무게(kg) + 6.25 × 키(cm) − 5 × 나이 − 161`
   - 498명의 건강한 성인(19~78세)으로 만든 식이에요. 2005년 미국영양사협회 체계적 문헌고찰에서 비교한 식 가운데 측정값과 10% 안으로 맞을 가능성이 가장 높았어요.
2. **평소 활동 (운동 제외)**: `RMR × PAL`
   - FAO/WHO/UNU(2004)의 PAL 범위는 다음과 같아요 **[근거]**.
     - 앉아서 생활·가벼운 활동: 1.40~1.69
     - 활동적인 생활: 1.70~1.99
     - 매우 활동적인 생활: 2.00~2.40
   - 앱은 각 범위 안에서 대표값을 골랐어요 **[앱 선택]**.
     - "주로 앉아서 지내요" 1.40 (앉아서 생활 범위의 하한)
     - "가볍게 자주 움직여요" 1.55 (같은 범위의 중간값)
     - "몸을 많이 쓰는 일을 해요" 1.75 (활동적 생활 범위의 하단)
3. **계획한 운동**: `(MET − 1) × 몸무게 × 1회 시간(시간) × 주당 횟수 ÷ 7`
   - MET 3.5는 2024 Adult Compendium of Physical Activities의 코드 02054 값이에요(근력 운동, 여러 동작, 8~15회) **[근거]**.
   - MET에서 1을 빼요. 운동 시간의 휴식 대사는 이미 2단계에 들어 있기 때문이에요. 그래서 운동 에너지가 **한 번만** 더해져요. 평소 활동량 선택지는 운동을 빼고 고르도록 안내해요.
   - "1 MET ≈ 몸무게 1kg당 시간당 1kcal"는 흔히 쓰는 근사예요. 이번 확인에서 원문은 찾지 못했어요.
4. **유지 열량** = 2단계 + 3단계
5. **목표에 따른 조정**
   - 체중 감량: 유지 열량에서 500kcal를 줄여요. 2013 AHA/ACC/TOS 비만 가이드라인은 하루 500~750kcal를 줄이거나 하루 여성 1,200~1,500kcal, 남성 1,500~1,800kcal를 먹는 방법을 제시해요 **[근거]**. 앱은 덜 공격적인 쪽인 500kcal를 써요.
   - 감량 목표의 최저치: 여성 1,200kcal, 남성 1,500kcal로 막아요. 유지 열량이 이보다 낮으면 줄이지 않아요.
   - 근육 증가: **유지 열량 그대로** 써요. Slater et al.(2019)는 근육 증가에 필요한 추가 열량이 알려져 있지 않다고 결론 내렸어요. 그래서 근거가 확인되지 않은 추가 열량은 넣지 않았어요. 대신 단백질을 높여요.
   - 그 밖의 목표(건강 유지, 체력 향상 등): 유지 열량을 그대로 써요.
   - 열량은 10kcal 단위로 반올림해요.
6. **탄수화물·단백질·지방**
   - 단백질: 감량·근육 증가는 체중 1kg당 1.6g, 그 외는 1.4g이에요.
     - ISSN 2017 입장문은 운동하는 사람에게 1.4~2.0g/kg를 권해요 **[근거]**.
     - Morton et al. 2018 메타분석에서는 1.6g/kg 근처에서 제지방 증가가 더 늘지 않았어요 **[근거]**.
   - 비율 범위: IOM(2005)의 권장 비율 범위(AMDR) 안에 들게 맞춰요 **[근거]**. 단백질 10~35%, 지방 20~35%, 탄수화물 45~65%예요.
   - 지방: 열량의 25%가 기본이에요 **[앱 선택, AMDR 범위 안]**. 탄수화물이 45~65%를 벗어나면 지방을 20~35% 안에서 조정해요.
   - 탄수화물: 나머지 열량으로 채워요. g당 탄수화물·단백질 4kcal, 지방 9kcal로 다시 더한 합계는 목표 열량과 반올림 오차(최대 ±4kcal, 보통 ±2kcal) 안에서 맞아요.

## 검증

저장 전에 `check_targets`가 아래 조건을 확인해요. 하나라도 어기면 목표를 만들지 않아요.
- 영양소 열량 합계가 목표 열량과 일치
- 세 영양소 비율이 모두 AMDR 안
- 감량 시 하루 차감이 750kcal 이하이고 최저 섭취량 이상
- 감량이 아닌 목표는 유지 열량과 같음
- 모든 영양소 값이 0보다 큼

## 목표를 만들지 않는 경우 (적용 범위)

- 만 19세 미만 또는 79세 이상: 계산식을 만든 자료의 나이 범위 밖이에요.
- 임신·수유 중, 또는 의료진의 식이 관리를 받는 중
- BMI 18.5 미만에서 감량을 목표로 하는 경우
- 계산 결과가 1,000~5,000kcal를 벗어나는 경우 **[앱 규칙]**

이 경우에는 이유를 보여주고 전문가 상담을 안내해요.

## 저장과 갱신

- `nutrition_targets` 테이블에 **이력**으로 쌓여요. 어떤 날짜의 목표는 "그 날짜 이전에 적용 시작된 가장 최근 값"이에요. 그래서 새 목표는 과거 날짜를 **소급해서 바꾸지 않아요**.
- 계산에 쓰이는 설정(키, 몸무게, 나이, 성별, 활동량, 상황, 목표, 운동 계획)이 바뀌어 저장될 때만 다시 계산해요. 새 목표는 **그날부터** 적용돼요. 같은 날 여러 번 바꾸면 마지막 값이 그날 목표가 되고, 식사 기록은 그대로 남아요.
- 화면을 열 때는 저장된 값만 읽어요. AI도 부르지 않아요. AI 설명은 목표가 새로 계산된 직후에 한 번 요청해요. 실패해도 계산된 목표는 그대로 쓰여요.
- 자동 계산 전에 직접 입력했던 목표는 "이전 직접 입력" 기록으로 보관돼요. 그래서 그 시기 날짜의 달성률은 바뀌지 않아요.
- 홈의 "오늘의 영양 목표", 식사 코치의 합계·달성률, 7일 그래프, AI 식단 피드백은 모두 이 저장값을 써요.

## 운동 루틴 주간 자동 조정

[backend/progression.py](backend/progression.py)가 맡아요. AI는 쓰지 않아요.

- 매주 처음 앱을 열 때 **지난주** "오늘의 운동 목표" 완료율을 봐요(루틴을 받은 날 이후만). 주마다 한 번만 반영해요.
- 완료율이 50% 미만이면 쉽게, 100%면 어렵게, 그 사이면 그대로 둬요 **[앱 규칙]**.
- 쉽게 할 때 **[앱 규칙]**: 강도를 한 단계 낮추고, 세트를 1개 줄이고, 횟수·시간을 약 20% 줄여요. 중량은 약 10%, 세션 시간은 약 15% 줄여요.
- 어렵게 할 때
  - 중량을 +5% 올려요(0.5kg 단위). ACSM 2009 입장문 "Progression Models in Resistance Training for Healthy Adults"에는 이렇게 적혀 있어요 **[근거, 원문 확인]**: *"a 2–10% increase in load be applied when the individual can perform the current workload for one to two repetitions over the desired number"*.
  - 앱은 "주간 운동을 모두 완료함"을 현재 부하를 소화할 수 있다는 신호로 봐요 **[앱 해석]**.
  - 0.5kg 단위로 올릴 수 없으면 횟수를 1회 늘려요. 중량이 없는 동작은 횟수 +2회, 시간 +10초예요. 강도는 한 단계 올려요.
- "루틴 다시 받기"(AI)를 누르면 지난주 완료율과 이전 루틴을 AI에 함께 보내요. 서버는 결과를 이전 루틴 기준으로 다시 제한해요(중량 최대 +10%, 낮출 때는 이전보다 무겁지 않게, 강도는 한 단계 안에서만 변경).

## 출처

- Mifflin MD, St Jeor ST, et al. A new predictive equation for resting energy expenditure in healthy individuals. *Am J Clin Nutr* 1990;51(2):241-7. doi:10.1093/ajcn/51.2.241
- Frankenfield D, Roth-Yousey L, Compher C. Comparison of predictive equations for resting metabolic rate in healthy nonobese and obese adults: a systematic review. *J Am Diet Assoc* 2005.
- FAO/WHO/UNU. *Human energy requirements* (2004). https://www.fao.org/4/Y5686e/y5686e07.htm
- Herrmann SD, et al. 2024 Adult Compendium of Physical Activities. *J Sport Health Sci* 2024. https://pacompendium.com
- Jensen MD, et al. 2013 AHA/ACC/TOS Guideline for the Management of Overweight and Obesity in Adults. *Circulation* 2014.
- Jäger R, et al. International Society of Sports Nutrition Position Stand: protein and exercise. *JISSN* 2017. https://pubmed.ncbi.nlm.nih.gov/28642676/
- Morton RW, et al. A systematic review, meta-analysis and meta-regression of the effect of protein supplementation on resistance training-induced gains in muscle mass and strength in healthy adults. *Br J Sports Med* 2018;52:376-84.
- Institute of Medicine. Dietary Reference Intakes for Energy, Carbohydrate, Fiber, Fat, Fatty Acids, Cholesterol, Protein, and Amino Acids (2005). https://www.nationalacademies.org/read/10490/chapter/27
- Slater GJ, et al. Is an energy surplus required to maximize skeletal muscle hypertrophy associated with resistance training? *Front Nutr* 2019. doi:10.3389/fnut.2019.00131
- American College of Sports Medicine. Progression Models in Resistance Training for Healthy Adults. *Med Sci Sports Exerc* 2009;41(3):687-708.

계산값은 모두 **추정치**예요. 실제 필요량은 사람마다 다를 수 있어요. 몸의 변화를 보며 조정하고, 질환이 있으면 전문가와 상의하세요.
