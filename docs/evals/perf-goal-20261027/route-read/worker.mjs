const expires = 1793126349068;
export default { async fetch(request, env) {
 if (Date.now() > expires || request.method !== "GET" || new URL(request.url).pathname !== "/measure") return new Response("Not found", {status:404});
 const rows=[];
 for (const kind of new URL(request.url).searchParams.has("reverse") ? ["conditional","get","head"] : ["get","conditional","head"]) {
  const metadata=await env.CONTROL.head("control/search-routing.json");
  if (!metadata || metadata.size>16384) return new Response("Unavailable",{status:503});
  const start=Date.now();
  if(kind==="head") {
   const object=await env.CONTROL.head("control/search-routing.json");
   rows.push({kind,ms:Date.now()-start,unchanged:object?.etag===metadata.etag});
  } else {
   const object=await env.CONTROL.get("control/search-routing.json",kind==="conditional"?{onlyIf:{etagDoesNotMatch:metadata.etag}}:undefined);
   if(!object) return new Response("Unavailable",{status:503});
   const headers_ms=Date.now()-start;
   if("body" in object) await object.json();
   rows.push({kind,ms:Date.now()-start,headers_ms,body:"body" in object,unchanged:object.etag===metadata.etag});
  }
 }
 return Response.json({rows},{headers:{"Cache-Control":"no-store"}});
} };
