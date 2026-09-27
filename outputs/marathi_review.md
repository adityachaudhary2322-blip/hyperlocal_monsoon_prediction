# Phase 6A - Marathi translations for review

Model: `sarvamai/sarvam-translate` (4-bit NF4, double-quant, bf16 compute; VRAM in use 4.13 of 6.00 GiB).

Translated from the **English** source: the open checkpoint supports English<->Indic only, so Hindi->Marathi would be an unsupported pivot.

| key | English | Marathi |
|---|---|---|
| `SOW_NOW` | Monsoon rain is likely to arrive within the next week. If your field is ready, sow {crops} now using your normal variety and spacing. Keep seed and fertiliser ready before the rain starts. | पुढच्या आठवड्यात मान्सूनचा पाऊस येण्याची शक्यता आहे. जर तुमचे शेत तयार असेल तर आताच तुमच्या सामान्य वाणाचे आणि अंतराचे {पिके} पेरा. पाऊस सुरू होण्यापूर्वी बियाणे आणि खत तयार ठेवा. |
| `DELAY_SOWING_10D` | Monsoon rain is unlikely in the next two weeks. Hold back sowing of {crops} for about 10 days and do not dry-sow. Use the time for field preparation, and keep the seed dry and ready. | पुढील दोन आठवड्यांत पावसाच्या सरी येण्याची शक्यता नाही. सुमारे १० दिवस {पिकांची} पेरणी थांबवा आणि कोरड पेरणी करू नका. वेळ शेतीची तयारीसाठी वापरा, बियाणे कोरडे आणि तयार ठेवा. |
| `SWITCH_SHORT_DURATION` | The sowing window for {crops} has passed and the monsoon is still late. Switch to a short-duration variety recommended by your KVK, treat the seed before sowing, and sow as soon as there is enough soil moisture. | {crops} पेरणीची वेळ निघून गेली आहे आणि अजूनही पावसाळा उशिरा आहे. तुमच्या के.व्ही.के.ने सुचविलेल्या अल्प-अवधीच्या वाणावर स्विच करा, पेरणीपूर्वी बियाण्यावर उपचार करा आणि जमिनीत पुरेसा ओलावा आल्यावर पेरणी करा. |
| `LIFE_SAVING_IRRIGATION_MULCH` | A long dry spell is likely in the coming weeks. Give {crops} one life-saving irrigation if water is available, spread crop residue as mulch between the rows, and hold back top-dressing of fertiliser until the soil has moisture again. A 2% urea or DAP spray can carry the crop through. | येत्या काही आठवड्यांत दीर्घकाळ कोरडे हवामान राहण्याची शक्यता आहे. पाणी उपलब्ध असल्यास पिकांना जीवदान देणारी एक सिंचन द्या, पिकांचे अवशेष रांगेमध्ये मल्च म्हणून पसरवा आणि मातीमध्ये पुन्हा ओलावा येईपर्यंत खताचा वरचा थर टाकू नका. २% युरिया किंवा डीएपी फवारणी पिकाला तगवून ठेवू शकते. |
| `DRAINAGE_POSTPONE_FERTILIZER_SPRAY` | Heavy rain is likely in the coming days. Open field channels and drain excess water from {crops}, and postpone fertiliser top-dressing and any spraying until the rain stops - it will wash off. Do interculture only once the soil is at workable moisture. | येत्या काही दिवसांत मुसळधार पाऊस पडण्याची शक्यता आहे. खुल्या शेतातील नाले आणि {पिकां}मधील अतिरिक्त पाणी काढून टाका आणि खत टाकणे आणि फवारणी थांबवा जोपर्यंत पाऊस थांबत नाही - तो वाहून जाईल. माती व्यवस्थित ओलावा असतानाच आंतरशेती करा. |
| `FLOOD_PREP` | Very heavy rain is likely and this district floods. Clear and deepen drainage channels now, stop any irrigation, and move harvested produce, seed and fertiliser to higher ground. On low land, prepare raised beds or plan to re-sow {crops} if water stands for more than two days. | अतिवृष्टीमुळे या जिल्ह्यात पूर येण्याची शक्यता आहे. जलनिस्सारण मार्ग आताच मोकळे आणि खोल करा, कोणतीही सिंचन व्यवस्था थांबवा आणि कापणी केलेले धान्य, बियाणे आणि खते उंच ठिकाणी हलवा. सखल जमिनीवर, उंच केलेल्या वाफ्या तयार करा किंवा दोन दिवसांपेक्षा जास्त काळ पाणी साचून राहिल्यास पुन्हा पेरणी करण्याची योजना करा. |
| `MONITOR` | No major rainfall risk is expected for {crops} in this period. Continue your normal package of practices and watch the next forecast. | या काळात {पिके} साठी पावसाचा मोठा धोका नाही. तुमच्या नेहमीच्या पद्धती चालू ठेवा आणि पुढील अंदाज पहा. |
| `conflict_note` | The forecast also shows a competing {other} risk at {other_pct}%. Treat this advisory as provisional and check the next update before spending on inputs. | हवामान अंदाज {other_pct}% वर {other} धोका दर्शवितो. हे सल्ले तात्पुरते समजून घ्या आणि इनपुटवर खर्च करण्यापूर्वी पुढील अपडेट तपासा. |
| `crop:arhar` | arhar | तूर |
| `crop:bajra` | bajra | बाजरी |
| `crop:cotton` | cotton | कापूस |
| `crop:groundnut` | groundnut | शेंगदाणे |
| `crop:jowar` | jowar | ज्वारी |
| `crop:maize` | maize | मका |
| `crop:moong` | moong | मूग |
| `crop:paddy` | paddy | भात |
| `crop:soybean` | soybean | सोयाबीन |
| `crop:sugarcane` | sugarcane | ऊस |
| `crop:til` | til | तिल |
| `crop:tur` | tur | टूर |
| `crop:urad` | urad | उडीद |
